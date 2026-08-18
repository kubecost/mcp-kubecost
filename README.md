# mcp-kubecost Helm Chart

A Helm chart for deploying the [mcp-kubecost](https://github.com/kubecost/mcp-kubecost) MCP server — a read-only FinOps MCP server for Kubecost analytics. Connect your AI assistant to Kubecost and ask natural-language questions about Kubernetes cloud costs and savings.

## Installation

```bash
helm install mcp-kubecost -n kubecost \
  --repo https://kubecost.github.io/mcp-kubecost mcp-kubecost
```

## Documentation

For complete configuration options, prerequisites, and advanced usage, see the [detailed chart documentation](https://github.com/kubecost/mcp-kubecost/blob/main/charts/mcp-kubecost/README.md).

## Maintainers

**IBM, Inc. All Rights Reserved.**
[https://ibm.com](https://ibm.com)

## License

Licensed under the Apache License, Version 2.0 (the "License")
