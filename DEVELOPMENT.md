# Development Setup - Environment Variables

## Quick Start for Local Development

1. Copy the environment template:
   ```bash
   cp .env.example .env
   ```

2. For local development without Azure Application Insights:
   - Keep `LOCAL_TRACING_ENABLED=true` in your `.env` file
   - This will use local tracing instead of remote Azure monitoring

3. For testing with Azure Application Insights:
   - Set `LOCAL_TRACING_ENABLED=false`
   - Get your Application Insights connection string from Azure Portal
   - Set `APPINSIGHTS_CONNECTIONSTRING` in your `.env` file

## Getting Application Insights Connection String

1. Go to your Azure Application Insights resource in the Azure Portal
2. In the overview page, find the "Connection String" field
3. Copy the entire connection string (it starts with `InstrumentationKey=...`)
4. Add it to your `.env` file:
   ```
   APPINSIGHTS_CONNECTIONSTRING="InstrumentationKey=xxx;IngestionEndpoint=https://xxx.in.applicationinsights.azure.com/;LiveEndpoint=https://xxx.livediagnostics.monitor.azure.com/"
   ```

## Environment Variables

- `LOCAL_TRACING_ENABLED`: Set to `true` to disable remote tracing and use local tracing instead
- `APPINSIGHTS_CONNECTIONSTRING`: Azure Application Insights connection string (only needed when `LOCAL_TRACING_ENABLED=false`)

## Troubleshooting

- **Error: "Instrumentation key cannot be none or empty"**
  - Either set `LOCAL_TRACING_ENABLED=true` for local development
  - Or provide a valid `APPINSIGHTS_CONNECTIONSTRING` value

- **Error: Environment variable not found**
  - Make sure you have a `.env` file in the project root
  - Check that the variable names match exactly (case sensitive)