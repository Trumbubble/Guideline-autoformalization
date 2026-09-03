#!/usr/bin/env bash
# Build the CQL toolchain (DEV_PLAN P6). Idempotent; safe to re-run.
#
#   cql-to-elm  (Java)  CQL text -> ELM JSON, with real FHIR type checking
#   cql-execution (JS)  ELM + FHIR bundle -> verdict.  It does NOT parse CQL,
#                       which is why the Java translator is unavoidable.
set -euo pipefail
cd "$(dirname "$0")"

CQF_VERSION="${CQF_VERSION:-3.26.0}"
ARCH="$(uname -m)"; OS="$(uname -s)"
case "$OS-$ARCH" in
  Darwin-arm64)  CS_ASSET=cs-aarch64-apple-darwin.gz ;;
  Darwin-x86_64) CS_ASSET=cs-x86_64-apple-darwin.gz ;;
  Linux-x86_64)  CS_ASSET=cs-x86_64-pc-linux.gz ;;
  Linux-aarch64) CS_ASSET=cs-aarch64-pc-linux.gz ;;
  *) echo "unsupported platform $OS-$ARCH; fetch coursier manually" >&2; exit 1 ;;
esac

command -v java >/dev/null || { echo "java is required (cql-to-elm is JVM-only)" >&2; exit 1; }
command -v npm  >/dev/null || { echo "npm is required (cql-execution is JS)" >&2; exit 1; }

# 1. coursier -- resolves the Maven dependency tree without installing Maven.
if [ ! -x ./cs ]; then
  echo "==> fetching coursier ($CS_ASSET)"
  curl -fLsS "https://github.com/coursier/coursier/releases/download/v2.1.24/$CS_ASSET" -o cs.gz
  gunzip -f cs.gz && chmod +x cs
fi

# 2. The four artifacts the translator actually needs. Each was discovered from
#    an error, in this order: cql-to-elm alone cannot read a model, cannot write
#    ELM, and has no FHIR model info.
if [ ! -s cp.txt ]; then
  echo "==> resolving cql-to-elm $CQF_VERSION and friends"
  ./cs fetch --classpath \
    "info.cqframework:cql-to-elm:$CQF_VERSION" \
    "info.cqframework:model-jackson:$CQF_VERSION" \
    "info.cqframework:elm-jackson:$CQF_VERSION" \
    "info.cqframework:quick:$CQF_VERSION" > cp.txt
fi

# 3. The driver. cql-to-elm ships as a library with no CLI.
if [ ! -f classes/Translate.class ] || [ Translate.java -nt classes/Translate.class ]; then
  echo "==> compiling Translate.java"
  javac -cp "$(cat cp.txt)" -d classes Translate.java
fi

# 4. FHIRHelpers -- every generated library includes it, so it must be
#    translatable alongside.
if [ ! -f FHIRHelpers.cql ]; then
  echo "==> fetching FHIRHelpers 4.0.1"
  curl -fLsS -o FHIRHelpers.cql \
    "https://raw.githubusercontent.com/AHRQ-CDS/AHRQ-CDS-Connect-PAIN-MANAGEMENT-SUMMARY/master/src/cql/r4/FHIRHelpers.cql"
fi
[ -f FHIRHelpers.json ] || { echo "==> translating FHIRHelpers"; \
  java -cp "classes:$(cat cp.txt)" Translate FHIRHelpers.cql FHIRHelpers.json 2>/dev/null; }

# 5. The execution engine.
[ -d node_modules/cql-execution ] || { echo "==> npm install"; npm install --silent cql-execution cql-exec-fhir; }

echo "==> ready:  $(tr ':' '\n' < cp.txt | wc -l | tr -d ' ') jars, cql-execution $(node -p "require('./node_modules/cql-execution/package.json').version")"
