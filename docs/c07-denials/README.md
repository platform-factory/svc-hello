# C-07(b): claims that must be denied

Three deliberately bad `Database` claims, each denied by a different rule in
the XRD schema (ADR-0014: the schema denies first). They are never applied
from `k8s/`; the test applies them by hand and records the message the
developer sees at each of the three surfaces: the CLI validator, the API
server, and Argo CD.

| File | What is wrong | Which rule denies it |
|---|---|---|
| `wrong-region.yaml` | `region: europe-west1` | the `region` enum |
| `oversized.yaml` | `size: XL` | the `size` enum |
| `cel-size-tier.yaml` | `size: L` with `tier: standard` | the CEL rule "size L only for critical" |

The oversized claim is also the answer to "what about a raw machine type?":
the schema has no field that could carry one, so `tier: db-custom-64-245760`
is an unknown field and is rejected before the enum is even consulted.
