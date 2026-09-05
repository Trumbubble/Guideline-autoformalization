# Deploying the demo

A reviewer opening the deployed link gets the dashboard, every pre-loaded
guideline, the checker with three-valued verdicts, and the formalisation
reports. To formalise a **new** document they paste their own Anthropic API
key — the server holds none.

## Why no server API key

`/settings` reports `server_has_api_key()`, and `guidelines._temporary_api_key()`
swaps a caller-supplied key in for the duration of one ingest. So the deployment
runs with `ANTHROPIC_API_KEY` unset:

* nobody can spend your credit, and there is nothing to rate-limit;
* a reviewer with a key (most have one) formalises any document they like;
* a reviewer without one still sees everything except live formalisation.

If you later decide to fund it, set the secret (`fly secrets set
ANTHROPIC_API_KEY=...`) — but add an access gate first, because the URL is
public and every upload costs you money.

## Deploy

```bash
brew install flyctl          # or: curl -L https://fly.io/install.sh | sh
fly auth signup              # or `fly auth login`

# Pick an app name that does not identify you -- the URL is <app>.fly.dev
# and reviewers see it. Edit `app = ...` in fly.toml first.
fly launch --no-deploy --copy-config --name <your-anonymous-name>
fly deploy
```

`fly deploy` uploads an ~849MB image (208MB of it is `terms.db`), so the
first push takes a few minutes. Afterwards:

```bash
fly open              # opens https://<app>.fly.dev
fly logs              # tail
fly status            # machine state
```

## What is and is not in the image

Baked in: the app, `terms.db` (634,485 concepts — SNOMED, LOINC, RxNorm, ATC,
VSAC), the corpus, and the pre-formalised guideline bundles.

Excluded on purpose (see `.dockerignore`):

| excluded | why it is safe |
|---|---|
| `CQL_converter/semantic/` (2.3GB) | `bind.py` degrades to lexical retrieval via `SemanticUnavailable`; retrieval recall drops ~6pp, nothing breaks |
| `patient_checker/engine/` (89MB) | the JVM+Node CQL toolchain; the paper's Limitations already state `cql-to-elm` has never run |
| `CQL_converter/loinc.db` | leftover staging artifact, nothing reads it |
| the raw SNOMED/LOINC/RxNorm releases | build-time input only, already loaded into `terms.db` |
| `.env` | **never ship this** |

## Uploads are ephemeral

`guidelines.STORE_DIR` is `patient_checker/guidelines/`, inside the container.
A machine restart loses anything a reviewer uploaded; the pre-loaded bundles
come back because they are in the image. That is usually what you want for a
demo. To persist uploads instead:

```bash
fly volumes create gf_data --size 1 --region lhr
```

then add to `fly.toml`:

```toml
[[mounts]]
  source = "gf_data"
  destination = "/app/patient_checker/guidelines"
```

## Licensing

`terms.db` contains SNOMED CT and RxNorm, which are UMLS-licensed per user.
The image is private to your Fly account and the app serves *lookups*, never
bulk download, which is ordinary use rather than redistribution. Do not push
this image to a public registry, and do not commit `terms.db` to git — it is
gitignored for exactly this reason (and at 208MB it exceeds GitHub's 100MB
per-file limit anyway).

## Anonymity for double-blind review

* the app name is the hostname — keep it neutral;
* `fly auth signup` with an address that is not your institutional one if the
  account name could surface anywhere;
* the dashboard shows guideline titles only, no author metadata.

## Verifying locally before you push

```bash
docker build -t guideline-checker .
docker run --rm -p 8099:8080 guideline-checker
curl localhost:8099/health        # {"status":"ok",...}
```
