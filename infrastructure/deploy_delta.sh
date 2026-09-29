#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy_delta.sh
# Push only what changed in this repo to AWS — minimal manual work.
#
# Examples:
#   ./deploy_delta.sh                  # auto-detect from git, deploy deltas
#   ./deploy_delta.sh --dry-run        # show plan, change nothing
#   ./deploy_delta.sh --base main      # diff against branch/commit (default: HEAD)
#   ./deploy_delta.sh digilux_ota_status_handler   # force these Lambda(s)
#   ./deploy_delta.sh --iam            # force IAM policy refresh
#   ./deploy_delta.sh --iot-rules      # force IoT rule refresh
#   ./deploy_delta.sh --cdk            # use CDK stacks instead of shell scripts
#
# What it deploys (auto from git path):
#   infrastructure/06_lambdas/<name>/   → update-function-code for that Lambda
#   infrastructure/05_iam_roles.sh
#   cdk/stacks/iam_stack.py             → IAM (shell or CDK)
#   infrastructure/08_iot_rules.sh      → IoT topic rules
#   Docs / make_test_artifact / Postman → skipped (not AWS runtime)
#
# Safe by default: Lambda code only (does NOT overwrite env vars / timeout).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Prevent AWS CLI from opening `less` and hanging the script on (END)
export AWS_PAGER=""
export PAGER=cat

REGION="${REGION:-ap-south-1}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LAMBDA_ROOT="$SCRIPT_DIR/06_lambdas"

DRY_RUN=0
USE_CDK=0
FORCE_IAM=0
FORCE_IOT=0
BASE_REF=""          # empty → unstaged+staged+untracked lambda dirs vs HEAD
EXPLICIT_LAMBDAS=()

usage() {
  sed -n '2,28p' "$0"
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)     usage ;;
    --dry-run)     DRY_RUN=1; shift ;;
    --cdk)         USE_CDK=1; shift ;;
    --iam)         FORCE_IAM=1; shift ;;
    --iot-rules)   FORCE_IOT=1; shift ;;
    --base)
      BASE_REF="${2:?--base needs a ref}"; shift 2 ;;
    digilux_ota_*|*-ota-*)
      EXPLICIT_LAMBDAS+=("$1"); shift ;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      ;;
  esac
done

cd "$REPO_ROOT"

if [[ $DRY_RUN -eq 0 ]]; then
  echo "==> AWS identity"
  ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
  echo "    Account=$ACCOUNT_ID  Region=$REGION"
  echo ""
else
  echo "==> Dry-run (skipping AWS identity check)  Region=$REGION"
  echo ""
fi

# ── Collect changed paths ─────────────────────────────────────────────────────
CHANGED_FILES=()

if [[ ${#EXPLICIT_LAMBDAS[@]} -eq 0 ]]; then
  if [[ -n "$BASE_REF" ]]; then
    echo "==> Diff vs $BASE_REF"
    while IFS= read -r f; do
      [[ -n "$f" ]] && CHANGED_FILES+=("$f")
    done < <(git diff --name-only "$BASE_REF"...HEAD 2>/dev/null; git diff --name-only; git diff --cached --name-only)
  else
    echo "==> Diff vs HEAD (working tree + staged)"
    while IFS= read -r f; do
      [[ -n "$f" ]] && CHANGED_FILES+=("$f")
    done < <(git diff --name-only HEAD; git diff --cached --name-only; git ls-files --others --exclude-standard)

    # If working tree is clean of AWS paths, also include commits not yet on origin/main
    # (typical: you committed locally, then want to deploy before/after push).
    _aws_in_wt=0
    for f in "${CHANGED_FILES[@]+"${CHANGED_FILES[@]}"}"; do
      case "$f" in
        infrastructure/06_lambdas/*|infrastructure/05_iam_roles.sh|infrastructure/08_iot_rules.sh|cdk/stacks/*)
          _aws_in_wt=1; break ;;
      esac
    done
    if [[ $_aws_in_wt -eq 0 ]]; then
      for _base in origin/master origin/main master main; do
        if git rev-parse --verify "$_base" >/dev/null 2>&1; then
          echo "==> Also scanning commits ahead of $_base"
          while IFS= read -r f; do
            [[ -n "$f" ]] && CHANGED_FILES+=("$f")
          done < <(git diff --name-only "$_base"...HEAD 2>/dev/null || true)
          break
        fi
      done
    fi
  fi
  # unique (bash 3.2 compatible — no mapfile)
  if [[ ${#CHANGED_FILES[@]} -gt 0 ]]; then
    CHANGED_FILES=($(printf '%s\n' "${CHANGED_FILES[@]}" | sort -u))
  fi
fi

NEED_IAM=0
NEED_IOT=0
NEED_CDK_LAMBDA=0
NEED_CDK_API=0
LAMBDAS=()
SKIPPED=()

add_lambda() {
  local n="$1"
  [[ -d "$LAMBDA_ROOT/$n" ]] || { echo "WARN: no source dir for $n — skip"; return; }
  local already=0
  for x in "${LAMBDAS[@]+"${LAMBDAS[@]}"}"; do
    [[ "$x" == "$n" ]] && already=1 && break
  done
  [[ $already -eq 0 ]] && LAMBDAS+=("$n")
}

if [[ ${#EXPLICIT_LAMBDAS[@]} -gt 0 ]]; then
  for n in "${EXPLICIT_LAMBDAS[@]}"; do
    add_lambda "$n"
  done
fi

for f in "${CHANGED_FILES[@]+"${CHANGED_FILES[@]}"}"; do
  case "$f" in
    infrastructure/06_lambdas/*/lambda_function.py|\
    infrastructure/06_lambdas/*/requirements.txt)
      dir=$(echo "$f" | cut -d/ -f3)
      add_lambda "$dir"
      ;;
    infrastructure/05_iam_roles.sh|cdk/stacks/iam_stack.py)
      NEED_IAM=1
      ;;
    infrastructure/08_iot_rules.sh)
      NEED_IOT=1
      ;;
    cdk/stacks/lambda_stack.py)
      NEED_CDK_LAMBDA=1
      NEED_IAM=1
      NEED_IOT=1
      ;;
    cdk/stacks/api_stack.py)
      NEED_CDK_API=1
      ;;
    *.md|postman/*|infrastructure/make_test_artifact.py|.gitignore|\
    infrastructure/deploy_*.sh|infrastructure/*_test*.sh|infrastructure/e2e*)
      SKIPPED+=("$f")
      ;;
    *)
      SKIPPED+=("$f")
      ;;
  esac
done

[[ $FORCE_IAM -eq 1 ]] && NEED_IAM=1
[[ $FORCE_IOT -eq 1 ]] && NEED_IOT=1

echo ""
echo "==> Deploy plan"
echo "    Lambdas : ${LAMBDAS[*]:-(none)}"
echo "    IAM     : $([[ $NEED_IAM -eq 1 ]] && echo YES || echo no)"
echo "    IoT rules: $([[ $NEED_IOT -eq 1 ]] && echo YES || echo no)"
if [[ $USE_CDK -eq 1 ]]; then
  echo "    Mode    : CDK"
  echo "    CDK lambda stack: $([[ $NEED_CDK_LAMBDA -eq 1 || ${#LAMBDAS[@]} -gt 0 ]] && echo YES || echo no)"
  echo "    CDK api stack   : $([[ $NEED_CDK_API -eq 1 ]] && echo YES || echo no)"
else
  echo "    Mode    : shell (update-function-code)"
fi
if [[ ${#SKIPPED[@]} -gt 0 ]]; then
  echo "    Skipped (not AWS runtime):"
  printf '      - %s\n' "${SKIPPED[@]}"
fi
echo ""

if [[ $DRY_RUN -eq 1 ]]; then
  echo "(dry-run) No changes applied."
  exit 0
fi

if [[ ${#LAMBDAS[@]} -eq 0 && $NEED_IAM -eq 0 && $NEED_IOT -eq 0 && $NEED_CDK_API -eq 0 ]]; then
  echo "Nothing AWS-related to deploy."
  echo "Tip: ./deploy_delta.sh digilux_ota_status_handler"
  echo "     ./deploy_delta.sh --base main"
  exit 0
fi

run() {
  echo "\$ $*"
  "$@"
}

# ── IAM ───────────────────────────────────────────────────────────────────────
if [[ $NEED_IAM -eq 1 ]]; then
  if [[ $USE_CDK -eq 1 ]]; then
    echo "==> CDK IAM stack"
    (cd "$REPO_ROOT/cdk" && run npx cdk deploy digilux-ota-iam -c env=digilux --require-approval never)
  else
    echo "==> IAM via 05_iam_roles.sh"
    run bash "$SCRIPT_DIR/05_iam_roles.sh"
  fi
  echo ""
fi

# ── Lambdas ───────────────────────────────────────────────────────────────────
deploy_one_lambda() {
  local name="$1"
  local src="$LAMBDA_ROOT/$name"
  local zip="/tmp/${name}.$$.zip"
  local pkg="$src/package"

  echo "==> Lambda: $name"

  if ! aws lambda get-function --function-name "$name" --region "$REGION" >/dev/null 2>&1; then
    echo "    ERROR: function $name does not exist in $REGION — create it first (07_deploy_lambdas / 13_user_ota_setup / CDK)."
    return 1
  fi

  rm -rf "$pkg" "$zip"
  if [[ -f "$src/requirements.txt" ]]; then
    echo "    Bundling requirements (manylinux)…"
    mkdir -p "$pkg"
    pip install -q \
      --platform manylinux2014_x86_64 \
      --python-version 3.11 \
      --only-binary=:all: \
      --implementation cp \
      -r "$src/requirements.txt" \
      -t "$pkg/" --upgrade
    cp "$src/lambda_function.py" "$pkg/"
    # Bundle sidecar config (user-facing copy, etc.)
    shopt -s nullglob
    for f in "$src"/*.json "$src"/messages.py; do
      [[ -f "$f" ]] && cp "$f" "$pkg/"
    done
    shopt -u nullglob
    (cd "$pkg" && zip -qr "$zip" .)
    rm -rf "$pkg"
  else
    (cd "$src" && zip -q "$zip" lambda_function.py messages.py messages.json 2>/dev/null) \
      || (cd "$src" && zip -q "$zip" lambda_function.py)
  fi

  run aws lambda update-function-code \
    --function-name "$name" \
    --zip-file "fileb://$zip" \
    --region "$REGION" >/dev/null

  run aws lambda wait function-updated --function-name "$name" --region "$REGION"
  local mod
  mod=$(aws lambda get-function-configuration \
    --function-name "$name" --region "$REGION" \
    --query 'LastModified' --output text)
  echo "    OK LastModified=$mod"
  rm -f "$zip"
}

if [[ $USE_CDK -eq 1 ]]; then
  if [[ ${#LAMBDAS[@]} -gt 0 || $NEED_CDK_LAMBDA -eq 1 ]]; then
    echo "==> CDK Lambda stack (deploys all Lambda assets in stack)"
    (cd "$REPO_ROOT/cdk" && run npx cdk deploy digilux-ota-lambda -c env=digilux --require-approval never)
  fi
  if [[ $NEED_CDK_API -eq 1 ]]; then
    echo "==> CDK API stack"
    (cd "$REPO_ROOT/cdk" && run npx cdk deploy digilux-ota-api -c env=digilux --require-approval never)
  fi
else
  for name in "${LAMBDAS[@]+"${LAMBDAS[@]}"}"; do
    deploy_one_lambda "$name"
    echo ""
  done
fi

# ── IoT rules ─────────────────────────────────────────────────────────────────
if [[ $NEED_IOT -eq 1 ]]; then
  if [[ $USE_CDK -eq 1 ]]; then
    echo "==> IoT rules live in digilux-ota-lambda stack under CDK — already covered if lambda stack deployed."
  else
    echo "==> IoT rules via 08_iot_rules.sh"
    run bash "$SCRIPT_DIR/08_iot_rules.sh"
  fi
  echo ""
fi

echo "==> Done"
echo "    Logs tip: aws logs tail /aws/lambda/<name> --since 5m --region $REGION"
