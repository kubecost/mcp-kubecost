# NetworkPolicy examples for Envoy Gateway

[envoy-httproute.values.yaml](envoy-httproute.values.yaml) is a Helm values overlay that enables the chart's HTTPRoute and NetworkPolicy. Combine it with your deployment's authentication settings; it does not choose an authentication mode or create a Gateway.

The example assumes:

- An existing Envoy-managed Gateway `finops/mcp-gateway` with an HTTPS listener named `https`, a certificate for `mcp.example.com`, and permission to attach this HTTPRoute.
- The MCP release and HTTPRoute in namespace `finops`.
- Envoy **data-plane proxy pods** in `envoy-gateway-system`, selected by `gateway.envoyproxy.io/owning-gateway-name` and `gateway.envoyproxy.io/owning-gateway-namespace`. The Gateway object's namespace and the proxy pods' namespace are separate settings.
- Cluster DNS pods labeled `k8s-app: kube-dns` in `kube-system`, and Kubecost frontend pods labeled `app: kubecost-frontend` in `kubecost`, serving TCP/9090.

Replace these example names, labels, ports and hostname with your deployment's values. In Envoy Gateway's Gateway Namespace mode, change the ingress `namespaceSelector` to the namespace containing the proxy pods. With merged Gateways, use the actual proxy labels for the owning GatewayClass instead. Check the running pods' labels; the control-plane controller is not the source of backend requests.

These selectors follow the current [Envoy Gateway deployment documentation](https://gateway.envoyproxy.io/docs/tasks/operations/deployment-mode/) and [Gateway Namespace mode documentation](https://gateway.envoyproxy.io/latest/tasks/operations/gateway-namespace-mode/), checked September 2026.

Preview from the repository root after editing the example and providing your deployment values:

```bash
helm template mcp-kubecost charts/mcp-kubecost \
  --namespace finops \
  -f /path/to/deployment.values.yaml \
  -f docs/examples/network-policies/envoy-httproute.values.yaml
```

The base example permits incoming MCP traffic from Envoy and outgoing DNS/Kubecost traffic. It deliberately permits no internet egress. The existing Gateway listener must allow routes from `finops`; if its proxy pods are themselves egress-isolated, their policy must also allow the MCP backend on TCP/3030.

Extend `egress` for each dependency:

| Dependency    | Rule needed                                                                                                           |
| ------------- | --------------------------------------------------------------------------------------------------------------------- |
| OIDC          | Discovery, authorization-server token endpoint and JWKS destinations, usually TCP/443. They can use different hosts.  |
| CIMD          | Client metadata and remote JWKS destinations, usually public TCP/443. No custom client-hostname allowlist is applied. |
| Telemetry     | Collector namespace/pod selectors and configured port (typically TCP/4317 or TCP/4318). Omit when telemetry is off.   |
| NodeLocal DNS | Your cluster's actual resolver destination; the kube-dns selector example may not cover it.                           |

For an external destination with stable addresses, copy the rule from [external-https.egress.yaml](external-https.egress.yaml) into `networkPolicy.egress`, replacing its documentation-only IP address. This file is a rule fragment, not a Helm overlay. Helm replaces lists rather than merging them; keep the DNS and Kubecost rules when adding egress.

Standard NetworkPolicy cannot allow hostnames. Supporting arbitrary public CIMD hosts requires a broader HTTPS egress rule, or a deployment-specific egress gateway/CNI policy with hostname support. An unrestricted TCP/443 rule also permits private HTTPS endpoints; keep FastMCP's SSRF protections enabled and choose the network scope deliberately. Account for IPv6 if your cluster uses it.

Policies require an enforcing CNI, and other policies can add permissions. Allowing the Envoy proxy does not filter the HTTP requests it forwards. Verify allowed and denied connections on the actual cluster, including OAuth login and CIMD metadata fetching; Helm rendering alone cannot prove enforcement. Local Docker and direct HTTP runs have no Kubernetes NetworkPolicy protection.

References checked for September 2026: [Kubernetes NetworkPolicy](https://kubernetes.io/docs/concepts/services-networking/network-policies/), [MCP transport security](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http), [FastMCP HTTP deployment](https://gofastmcp.com/deployment/http).
