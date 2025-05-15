# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""GitHub Action to evaluate Azure AI agents using the Azure AI Evaluation SDK."""

import inspect
import json
import os
import random
import time
import uuid
from pathlib import Path

import azure.ai.evaluation as evals
import pandas as pd
import yaml
from azure.ai.evaluation import AIAgentConverter, evaluate
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import Agent, ConnectionType, MessageRole, RunStatus
from azure.identity import DefaultAzureCredential

import analysis

# NOTE: custom evaluators must be imported so evaluate() can pickle them

current_dir = Path(__file__).parent
env_path = current_dir / ".env"
if env_path.exists():
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=env_path)

STEP_SUMMARY = os.getenv("GITHUB_STEP_SUMMARY") or os.getenv("ADO_STEP_SUMMARY")

AZURE_AIPROJECT_CONNECTION_STRING = os.getenv("AZURE_AIPROJECT_CONNECTION_STRING")
DEPLOYMENT_NAME = os.getenv("DEPLOYMENT_NAME")
API_VERSION = os.getenv("API_VERSION")
DATA_PATH = os.getenv("DATA_PATH")
AGENT_IDS = [x.strip() for x in os.getenv("AGENT_IDS", "").split(",") if x.strip()]
BASELINE_AGENT_ID = os.getenv("BASELINE_AGENT_ID")
EVALUATION_RESULT_VIEW = os.getenv("EVALUATION_RESULT_VIEW")


# pylint: disable=too-many-locals
def simulate_question_answer(
    project_client: AIProjectClient, agent: Agent, input_queries: dict
) -> dict:
    """
    Simulates a question-answering interaction with an agent.

    This function performs the following steps:
    1. Creates a new thread for the interaction.
    2. Sends a user message containing the query to the agent.
    3. Processes the run to generate the agent's response.
    4. Handles retries with exponential backoff in case of rate limit errors.
    5. Extracts the agent's response and relevant metrics.

    Args:
        project_client (AIProjectClient): The client used to interact with the Azure AI Project.
        agent (Agent): The agent instance to simulate the interaction with.
        input_queries (dict): A dictionary containing the input data for the interaction.
                      It must include a "query" key and may include "id".

    Returns:
        dict: A dictionary containing the evaluation input using thread data with added fields:
            - "id": The unique identifier for the input.
            - "metrics": A dictionary of performance metrics.

    Raises:
        ValueError: If the run fails for reasons other than rate limits or if retries are exhausted.
    """
    thread = project_client.agents.create_thread()
    project_client.agents.create_message(
        thread.id, role=MessageRole.USER, content=input_queries["query"]
    )

    # Exponential backoff retry logic
    max_retries = 5
    base_wait_seconds = 2
    for attempt in range(max_retries):
        start_time = time.time()
        run = project_client.agents.create_and_process_run(
            thread_id=thread.id, agent_id=agent.id
        )
        end_time = time.time()

        if run.status == RunStatus.COMPLETED:
            break

        if run.last_error.code == "rate_limit_exceeded" and attempt < max_retries - 1:
            # Calculate wait time with exponential backoff (2^attempt * base_wait_seconds)
            # with a small random jitter to avoid thundering herd problem
            jitter = random.uniform(0, 0.5)
            wait_seconds = (2**attempt) * base_wait_seconds + jitter
            print(
                f"Rate limit exceeded (attempt {attempt + 1}/{max_retries}). "
                f"You may wish to increase your quota. "
                f"Retrying in {wait_seconds: .2f} seconds..."
            )
            time.sleep(wait_seconds)
        else:
            if run.status != RunStatus.COMPLETED:
                raise ValueError(run.last_error or "Run failed to complete")

    if run.status != RunStatus.COMPLETED:
        raise ValueError(f"Failed to complete run after {max_retries} attempts")

    # Collect performance metrics
    metrics = {
        "server-run-duration-in-seconds": (
            run.completed_at - run.created_at
        ).total_seconds(),
        "client-run-duration-in-seconds": end_time - start_time,
        "completion-tokens": run.usage.completion_tokens,
        "prompt-tokens": run.usage.prompt_tokens,
    }

    # Generate evaluation data from the thread
    converter = AIAgentConverter(project_client)
    evaluation_data = converter.prepare_evaluation_data(thread_ids=thread.id)

    output = evaluation_data[0]
    output["id"] = input_queries.get(
        "id", str(uuid.uuid4())
    )  # Use provided ID or generate one
    output["metrics"] = metrics

    return output


def create_evaluators(class_names: list[str], args_default: dict) -> dict:
    """
    Creates a dictionary of evaluators based on the provided class names and default arguments.
    This function reads evaluator metadata from a YAML file, matches the provided class names
    with the metadata, and dynamically creates instances of the corresponding evaluator classes.
    It also appends a custom evaluator for operational metrics.
    Args:
        class_names (list[str]): A list of evaluator class names to be instantiated.
        args_default (dict): A dictionary containing default arguments to be used for initializing
            the evaluator classes.
    Returns:
        dict: A dictionary where the keys are evaluator keys (from the metadata) and the values
        are instances of the corresponding evaluator classes.
    Raises:
        AttributeError: If the specified evaluator class is not found in the `evals` module.
        KeyError: If a required argument for an evaluator class is missing in `args_default`.
    """
    path = Path(__file__).parent / "analysis" / "evaluator-scores.yaml"
    with open(path, encoding="utf-8") as f:
        evaluator_metadata = yaml.safe_load(f)

    evaluators = {}
    for evaluator_search in class_names:
        evaluator_found = None
        for section in evaluator_metadata["sections"]:
            for evaluator in section["evaluators"]:
                if evaluator["class"] == evaluator_search:
                    evaluator_found = evaluator
                    break

        if not evaluator_found:
            print(f"Unrecognized evaluator '{evaluator_search}'")
            continue

        # create evaluator instance using class from evals module
        evaluator_class = getattr(evals, evaluator_found["class"])
        init_signature = inspect.signature(evaluator_class.__init__)
        args_required = {
            k
            for k, v in init_signature.parameters.items()
            if (
                v.kind is v.POSITIONAL_OR_KEYWORD
                and k != "self"
                and v.default is v.empty
            )
        }
        args_used = {k: args_default[k] for k in args_required}

        evaluators[evaluator_found["key"]] = evaluator_class(**args_used)

    # append custom evaluator to propagate operational metrics to evaluation result
    evaluators["operational_metrics"] = analysis.OperationalMetricsEvaluator()

    return evaluators


# pylint: disable=too-many-branches
def validate_input_data(data: dict) -> None:
    """
    Validates that the input data has the required structure and fields.

    Args:
        data: The input data to validate

    Raises:
        ValueError: If the input data is missing required fields or has invalid types
    """
    # Validate required fields in the input data
    required_fields = ["name", "evaluators", "data"]
    missing_fields = [field for field in required_fields if field not in data]

    if missing_fields:
        raise ValueError(
            f"Input data is missing required fields: {', '.join(missing_fields)}"
        )

    # Validate field types
    if not isinstance(data["name"], str):
        raise ValueError("Input data 'name' must be a string")

    if not isinstance(data["evaluators"], list):
        raise ValueError("Input data 'evaluators' must be a list")

    if not isinstance(data["data"], list):
        raise ValueError("Input data 'data' must be a list")

    if not data["data"]:
        raise ValueError("Input data 'data' list cannot be empty")

    # Validate that each item in data has a 'query' field and check for unique IDs
    ids = set()
    for i, item in enumerate(data["data"]):
        if not isinstance(item, dict):
            raise ValueError(f"Item at index {i} in 'data' must be a dictionary")
        if "query" not in item:
            raise ValueError(
                f"Item at index {i} in 'data' is missing required field 'query'"
            )
        # Check if ID is provided, and ensure it's unique
        if "id" in item:
            if item["id"] in ids:
                raise ValueError(f"Duplicate ID '{item['id']}' found in 'data'")
            ids.add(item["id"])

    # Validate that all evaluator names exist in the available evaluators
    available_evaluators = []
    evaluator_path = Path(__file__).parent / "analysis" / "evaluator-scores.yaml"
    with open(evaluator_path, encoding="utf-8") as f:
        metadata = yaml.safe_load(f)
        for section in metadata["sections"]:
            for evaluator in section["evaluators"]:
                available_evaluators.append(evaluator["class"])

    unknown_evaluators = [
        e
        for e in data["evaluators"]
        if e not in available_evaluators and e != "OperationalMetricsEvaluator"
    ]
    if unknown_evaluators:
        raise ValueError(
            f"Unknown evaluators specified: {', '.join(unknown_evaluators)}"
        )


# pylint: disable=too-many-locals, too-many-arguments, too-many-positional-arguments
def main(
    credential,
    conn_str: str,
    input_data_set: dict,
    agent_ids: list[str],
    baseline_agent_id: str | None = None,
    working_dir: Path | None = None,
    eval_result_view: analysis.EvaluationResultView = analysis.EvaluationResultView.DEFAULT,
) -> str:
    """
    Main function to evaluate AI agents using simulated conversations and analysis.

    Args:
        credential: The credential object for authentication.
        conn_str (str): The connection string for the AI project.
        input_data_set (dict): The input data containing evaluation details, including
            the dataset and evaluator configurations.
        agent_ids (list[str]): A list of agent IDs to be evaluated.
        baseline_agent_id (Optional[str], optional): The ID of the baseline agent for
            comparison. Defaults to the first agent in `agent_ids` if not provided.
        working_dir (Path, optional): The working directory for storing intermediate
            evaluation files. Defaults to the current directory.
        eval_result_view (analysis.EvaluationResultView, optional): The view type for
            displaying evaluation results. Defaults to `EvaluationResultView.DEFAULT`.

    Returns:
        str: A summary of the evaluation results, including analysis and comparison
        of agents' performance.

    Raises:
        Exception: If any error occurs during the simulation of question-answer
        interactions or evaluation process.

    Notes:
        - The function uses the default evaluator model configuration.
        - Evaluation results are stored in JSON files in the specified working
          directory.
        - The function facilitates paired comparisons by adding unique IDs to input
          data rows if not already present.
        - The evaluation results are analyzed and summarized, with a baseline agent
          used for comparison.
    """
    working_dir = Path(".") if working_dir is None else working_dir
    project_client = AIProjectClient.from_connection_string(
        conn_str, credential=credential
    )

    # use default evaluator model config
    default_connection = project_client.connections.get_default(
        connection_type=ConnectionType.AZURE_OPEN_AI, include_credentials=True
    )
    model_config = default_connection.to_evaluator_model_config(
        deployment_name=DEPLOYMENT_NAME,
        api_version=API_VERSION or "",
        include_credentials=True,
    )

    agents = {id: project_client.agents.get_agent(id) for id in agent_ids}
    eval_input_paths = {id: working_dir / f"eval-input_{id}.jsonl" for id in agent_ids}
    eval_output_paths = {id: working_dir / f"eval-output_{id}.json" for id in agent_ids}

    # facilitate paired comparisons by adding GUIDs to input data
    for row in input_data_set["data"]:
        if "id" not in row:
            row["id"] = str(uuid.uuid4())

    # simulate conversations with each agent to produce evaluation inputs
    for agent_id, agent in agents.items():
        eval_input_paths[agent_id].unlink(missing_ok=True)
        for row in input_data_set["data"]:
            try:
                eval_input = simulate_question_answer(project_client, agent, row)
                with eval_input_paths[agent_id].open("a", encoding="utf-8") as f:
                    f.write(json.dumps(eval_input) + "\n")
            # pylint: disable=broad-exception-caught
            except Exception as e:
                print(
                    f"An error occurred while simulating question-answer for agent {agent_id}: {e}"
                )

    # create evaluator instances
    args_default = {
        "model_config": model_config,
        "credential": credential,
        "azure_ai_project": project_client.scope,
        "rouge_type": evals.RougeType.ROUGE_L,
    }
    evaluators = create_evaluators(input_data_set["evaluators"], args_default)

    # evaluate locally
    for agent_id, agent in agents.items():
        eval_name = (
            f"Evaluating agent '{agent.name}' upon dataset '{input_data_set['name']}'"
        )
        evaluate(
            data=eval_input_paths[agent_id],
            evaluators=evaluators,
            evaluation_name=eval_name,
            azure_ai_project=project_client.scope,
            output_path=eval_output_paths[agent_id],
        )
        # display evaluation results
        print(f"Evaluation results for agent '{agent.name}': ")

    # analyze evaluation results
    eval_results = {}
    for agent_id, agent in agents.items():
        with open(eval_output_paths[agent_id], encoding="utf-8") as f:
            eval_result_data = json.load(f)

        eval_rows = convert_pass_fail_to_boolean(eval_result_data)

        eval_results[agent_id] = analysis.EvaluationResult(
            variant=agent.name,
            ai_foundry_url=eval_result_data["studio_url"],
            df_result=pd.DataFrame.from_records(eval_rows),
        )

    baseline_agent_id = baseline_agent_id or agent_ids[0]
    project_scope = project_client.scope
    agent_base_url = (
        f"https://ai.azure.com/playground/agents?"
        f"wsid=/subscriptions/{project_scope['subscription_id']}/"
        f"resourceGroups/{project_scope['resource_group_name']}/"
        f"providers/Microsoft.MachineLearningServices/workspaces/"
        f"{project_scope['project_name']}&assistantId="
    )

    return analysis.summarize(
        eval_results,
        agents,
        baseline_agent_id,
        input_data_set["evaluators"] + ["OperationalMetricsEvaluator"],
        agent_base_url,
        eval_result_view,
    )


def convert_pass_fail_to_boolean(eval_result_data):
    """Convert "pass" and "fail" strings in evaluation results to booleans."""
    eval_rows = eval_result_data["rows"]
    for row in eval_rows:
        for key in row:
            # Check if the field is an outputs field
            if key.startswith("outputs."):
                # If the value is a string, check for "pass" or "fail"
                if isinstance(row[key], str):
                    if row[key].lower() == "pass":
                        row[key] = True
                    elif row[key].lower() == "fail":
                        row[key] = False
    return eval_rows


if __name__ == "__main__":
    # Check required environment variables
    if not AZURE_AIPROJECT_CONNECTION_STRING:
        raise ValueError(
            "AZURE_AIPROJECT_CONNECTION_STRING environment variable is not set"
        )
    if not DEPLOYMENT_NAME:
        raise ValueError("DEPLOYMENT_NAME environment variable is not set or empty")
    if not DATA_PATH:
        raise ValueError("DATA_PATH environment variable is not set")
    if not AGENT_IDS:
        raise ValueError("AGENT_IDS environment variable is not set or empty")

    # Check optional environment variables
    if BASELINE_AGENT_ID and BASELINE_AGENT_ID not in AGENT_IDS:
        raise ValueError(
            f"BASELINE_AGENT_ID '{BASELINE_AGENT_ID}' is not in AGENT_IDS '{AGENT_IDS}'"
        )

    result_view = analysis.EvaluationResultView.DEFAULT
    if EVALUATION_RESULT_VIEW:
        try:
            result_view = analysis.EvaluationResultView(EVALUATION_RESULT_VIEW)
        except ValueError as exc:
            valid_options = [e.value for e in analysis.EvaluationResultView]
            raise ValueError(
                f"EVALUATION_RESULT_VIEW must be one of {valid_options}"
            ) from exc

    # Load and validate input data
    try:
        input_data_path = Path(DATA_PATH)
        input_data = json.loads(input_data_path.read_text(encoding="utf-8"))
        validate_input_data(input_data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Input data at {DATA_PATH} is not valid JSON") from exc

    # Run evaluation and output summary
    SUMMARY_MD = main(
        credential=DefaultAzureCredential(),
        conn_str=AZURE_AIPROJECT_CONNECTION_STRING,
        input_data_set=input_data,
        agent_ids=AGENT_IDS,
        baseline_agent_id=BASELINE_AGENT_ID,
        working_dir=input_data_path.parent,
        eval_result_view=result_view,
    )

    if STEP_SUMMARY:
        with open(STEP_SUMMARY, "a", encoding="utf-8") as fp:
            fp.write(SUMMARY_MD)

    # REMOVE ME
    if env_path.exists():
        with open(Path(".") / "evaluation.md", "a", encoding="utf-8") as fp:
            fp.write(SUMMARY_MD)
