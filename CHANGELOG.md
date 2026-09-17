# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Package, Helm chart, and git tags currently use **0.17.0** (`v0.17.0`). That is the GA baseline. Later 1.x versions belong under `[Unreleased]` until they are tagged.

## [Unreleased]

### Changed

- Default container image tag is now the git/ICR tag with a leading `v` (`icr.io/kubecost/mcp-kubecost:vX.Y.Z`). Helm chart `version` is still unprefixed SemVer. The unprefixed image tag is no longer published on new releases.

## [0.17.0] - 2026-09-16

General availability of the Kubecost FinOps MCP server: a read-only MCP interface over Kubecost cost allocation and optimization APIs.

### Added

- Eleven read-only MCP tools for workload costs, cost comparison, container request-sizing candidates, abandoned workloads, savings overview, PV and local-disk sizing, cluster rightsizing, unclaimed volumes, and resource-quota recommendations
- MCP prompts, skills, and resources for client workflows (including container sizing profiles and the rightsizing review)
- Helm chart for Kubernetes, with OIDC and API-key authentication
- STDIO and Streamable HTTP transports, plus `/health` and `/version` HTTP routes
- Optional OpenTelemetry extra, installed in the container image

[Unreleased]: https://github.com/kubecost/mcp-kubecost/compare/v0.17.0...HEAD
[0.17.0]: https://github.com/kubecost/mcp-kubecost/releases/tag/v0.17.0
