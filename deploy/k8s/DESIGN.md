# SentinelDeployment — Kubernetes Operator Design (not implemented)

**Status: design only.** Nothing under `deploy/k8s/` is a working operator.
The manifests in `manifests/` are placeholders marked WIP. This document is
the Tier 3 deliverable: a real CRD design a platform team could implement in
a sprint, not a half-built controller they'd have to reverse-engineer.

## Why this exists

Everything Sentinel ships today runs as a CLI (`sentinel quality run`,
`sentinel ml-check`) or a GitHub Action. That's the right shape for a demo
and for small teams — but a platform team operating DataHub for a whole
company wants to declare *"run these Sentinel checks against this DataHub,
on this schedule, notifying this channel"* once, in Git, and have the
cluster keep it true. That's an operator pattern: a `SentinelDeployment`
custom resource plus a controller that reconciles it into CronJobs.

## The custom resource

```yaml
apiVersion: sentinel.datahub.io/v1alpha1
kind: SentinelDeployment
metadata:
  name: commerce-sentinel
  namespace: data-platform
spec:
  datahub:
    gmsUrl: http://datahub-gms.datahub.svc:8080
    # Secret must contain key `token`; never an inline credential.
    tokenSecretRef:
      name: datahub-credentials
      key: token

  anthropic:
    # Only needed if `agents.enrichment.enabled: true` (the one agent here
    # that calls an LLM). Migration Copilot/PR Impact are CI-side, not
    # operator-side, so no GitHub credentials appear in this CRD.
    apiKeySecretRef:
      name: anthropic-credentials
      key: api-key
    model: claude-opus-4-8   # pass-through to ANTHROPIC_MODEL

  agents:
    qualityChecker:
      enabled: true
      schedule: "*/30 * * * *"          # cron, evaluated in the controller's TZ
      checksConfigMapRef:                # quality_checks.yml lives in Git -> ConfigMap
        name: sentinel-quality-checks
        key: quality_checks.yml
      mode: ingestion                    # ingestion | warehouse
    mlBlastRadius:
      enabled: true
      schedule: "0 * * * *"
      urns:                              # the assets to re-check each run
        - "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detection_v3,PROD)"
      hopLimit: 6
    enrichment:
      enabled: false                     # off by default: LLM cost + human review queue

  notifications:
    slack:
      webhookSecretRef:
        name: slack-credentials
        key: webhook-url
      channel: "#data-incidents"

  severityRulesConfigMapRef:             # config/severity_rules.yml -> ConfigMap
    name: sentinel-severity-rules
    key: severity_rules.yml

status:                                  # written by the controller, never by users
  conditions:
    - type: Ready
      status: "True"
      reason: AllCronJobsReconciled
  lastRunTimes:
    qualityChecker: "2026-08-01T12:30:00Z"
    mlBlastRadius: "2026-08-01T12:00:00Z"
  managedResources:
    - kind: CronJob
      name: commerce-sentinel-quality-checker
    - kind: CronJob
      name: commerce-sentinel-ml-blast-radius
```

## Reconciliation loop (sketch)

```
watch SentinelDeployment (+ owned CronJobs, ConfigMaps, Secrets by ref)
  └─ for each event:
     1. Build the DESIRED state:
        one CronJob per enabled agent, running the Sentinel image with
        `sentinel quality run` / `sentinel ml-check --urn ...`, env from
        the CRD's secretRefs/configMapRefs, schedule from spec.
     2. Read the ACTUAL state: list CronJobs labeled
        sentinel.datahub.io/owner=<name>.
     3. Diff desired vs actual:
        - missing        -> create (with ownerReference to the CR, so
                            deleting the CR garbage-collects everything)
        - drifted        -> patch (schedule, image, env, args)
        - orphaned       -> delete (agent was disabled)
     4. Write .status: conditions, lastRunTimes (from the CronJobs' last
        successful Job), managedResources.
     5. Requeue on error with exponential backoff; also requeue every 10m
        as a resync safety net (level-triggered, not edge-triggered).
```

Idempotency note: the whole loop must be a pure function of (spec, cluster
state) — the same discipline as the Incident Engine's dedup key. A
controller that "remembers" what it did last time is a controller that
drifts.

## Why it's out of scope for this hackathon

Building the CRD + controller skeleton is a day; building an operator
someone can *trust* is not. The real cost is in: a multi-cluster/multi-
version test surface (envtest + kind matrices per K8s version), RBAC design
narrow enough to pass a security review (the controller needs CronJob
create/delete — a scary verb pair to scope correctly), an admission webhook
to validate CRs at apply time (schedule syntax, secretRef existence) rather
than at reconcile time, and upgrade/migration paths for the CRD schema
itself (`v1alpha1 -> v1beta1` conversion webhooks). None of that improves
the three Tier 1 features this project is judged on, so it's specified here
and deliberately not built. The 30-day budget went to the agents.

## What implementing it would look like

- **Framework**: `kopf` (Python, matches this repo) for a fast start, or
  kubebuilder/controller-runtime (Go) if it should eventually live with
  DataHub's own `datahub-helm` ecosystem.
- **Image**: this repo's Dockerfile already produces a CLI image; the
  CronJobs just call it.
- **First milestone**: qualityChecker only, no status subresource, kind
  cluster in CI running the seeded demo — that's a weekend-sized start a
  contributor could pick up from this doc alone.
