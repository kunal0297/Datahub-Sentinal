# Demo Video Script — DataHub Sentinel

**Target runtime: 2:30** (hard limit 3:00 — leave margin). Record against
the real running demo (`make demo`), never mockups. One take per scene is
fine; cut in post. Screen at 1080p, DataHub UI zoomed to ~110% so entity
names are legible on a phone.

**Pre-record checklist**

- [ ] Fresh `make demo` completed; DataHub UI logged in (datahub/datahub)
- [ ] `.env` has `ANTHROPIC_API_KEY` (real codegen on camera, not the stub)
- [ ] Terminal: big font, dark theme, `clear` before each scene
- [ ] Browser tabs pre-opened in order: DataHub home → `analytics.orders_v1`
      entity page → `fraud_detection_v3` entity page → Incidents tab
- [ ] A scripted breaking-change branch ready in `seed/sample_repo`
      (drop `discount_pct` from `models/orders_v1.sql`, committed)
- [ ] Rehearse once with a timer; if over 2:40, cut Scene 4's narration first

---

## Scene 0 — Cold open (0:00–0:15)

**Screen:** DataHub lineage view of `analytics.orders_v1` showing the
dashboards + ML chain fanning out.

**Narration:**
> "DataHub knows what your data means and what depends on it. But it can't
> stop the pull request that's about to break all of this. DataHub Sentinel
> is the insertion point — agents that act at the moment of breakage and
> write what they learn back into the graph."

## Scene 1 — PR Impact Analysis (0:15–0:55)

**Screen:** terminal. Run:

```bash
python -m sentinel.cli pr-impact --repo seed/sample_repo --base-ref HEAD~1 \
  --pr-link https://github.com/acme/analytics/pull/482
```

Scroll the printed comment slowly (it's the same body the GitHub Action
posts). Point at the CRITICAL banner, the owners column, the model row.

**Narration:**
> "A PR drops one column from a dbt model. Sentinel resolves the file to
> its DataHub URN, diffs the schema against the catalog, walks downstream
> lineage — two executive dashboards, a revenue rollup, and a production
> fraud model — resolves their owners, classifies it CRITICAL, and posts
> one idempotent PR comment."

**Cut to:** DataHub UI, `analytics.orders_v1` → Incidents tab, the new
incident open, description visible.

> "And it raised a real DataHub incident — with which agent raised it, why,
> and a link back to the PR. Run it twice: no duplicates. The engine dedups
> by root cause."

## Scene 2 — Schema Migration Copilot (0:55–1:35)

**Screen:** terminal. Run:

```bash
python -m sentinel.cli migrate --from <orders_v1 urn> --to <orders_v2 urn> \
  --repo seed/sample_repo
```

Show the printed column mapping first (pause ~2s), then the generated
patch (`git diff --no-index` the rewritten file), then:

```bash
python -m sentinel.cli migrate status --repo seed/sample_repo
```

**Narration:**
> "Migrating orders_v1 to v2. Sentinel pulls both real schemas from
> DataHub, infers the column mapping — and prints it for review before
> touching anything. Then it finds every consumer via lineage and has
> Claude rewrite each one, constrained to the DataHub-verified mapping:
> the model is never allowed to invent a column. One reviewable patch per
> consumer, one tracker for the whole migration."

**Cut to:** DataHub UI, `orders_v1` entity page showing the deprecation
banner: "Superseded by orders_v2".

> "It finishes by writing the deprecation back to DataHub — so the next
> PR that touches orders_v1 gets warned automatically."

## Scene 3 — ML Blast Radius + auto-resolving incidents (1:35–2:10)

**Screen:** terminal. Run:

```bash
python -m sentinel.cli ml-check --urn <raw.orders urn>
```

Highlight the path line with the cursor.

**Narration:**
> "raw.orders failed its freshness check overnight. Nobody's dashboard is
> broken yet — but Sentinel traces the dependency path five hops across
> feature tables to fraud_detection_v3, which is serving production
> traffic, and raises the incident on the model — where the ML on-call
> will actually see it — with the exact path in the description."

**Screen:** terminal, quick cut:

```bash
python -m sentinel.cli quality run          # fails, incident
python seed/seed_datahub.py --heal
python -m sentinel.cli quality run          # passes, auto-resolves
```

> "Quality checks are code, results are native DataHub assertions — and
> when the data is fixed, Sentinel closes its own incident and says why."

## Scene 4 — The graph is richer (2:10–2:30)

**Screen:** DataHub UI: Incidents list → assertion history on raw.orders →
deprecation banner → an accepted enrichment description.

**Narration:**
> "Everything Sentinel learned is now in DataHub: incidents with root
> causes, assertion history, deprecation links, human-approved docs. Three
> features built fully, three honest MVPs, and every extension point a
> platform team needs — open source, Apache 2.0, and runnable with one
> command. DataHub Sentinel."

**End card (2 s):** repo URL + "make demo".

---

## Cut-for-time priority

If the edit lands over 3:00, cut in this order:
1. Scene 3's quality-checker sub-beat (keep ml-check)
2. Scene 1's dedup line
3. Scene 4 narration down to one sentence

Never cut: the PR comment scroll, the mapping review pause, the deprecation
banner, the final incidents view — those are the four judged write-backs.
