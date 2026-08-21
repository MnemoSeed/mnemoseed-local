#!/bin/sh
# install.sh - MnemoSeed Local zero-dependency install orchestrator (Linux/macOS, POSIX sh).
#
# One-line entry:
#   curl -fsSL https://raw.githubusercontent.com/MnemoSeed/mnemoseed-local/main/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- [--dry-run] [--yes] [--tier lite|standard|advanced]
# Or run as a file:
#   sh ./install.sh [--dry-run] [--yes] [--tier <lite|standard|advanced>]
#
# Orchestration order (identical to install.ps1):
#   1. detect / install ollama   (Linux: official install script; macOS/other:
#      print an install hint and exit non-zero)
#   2. detect / install uv       (official installer; well-known install dir is
#      prepended to the current process PATH)
#   3. install / upgrade the CLI (uv tool install | uv tool upgrade)
#   4. mnemoseed-local init      (skipped when ~/.mnemoseed-local/config.toml exists)
#   5. mnemoseed-local doctor    (verbatim) + hardware-tier hint; hint-only,
#      the script never changes config keys itself
#   6. ollama pull <dream model> (the dream route's model from config.toml
#      [dream.llm.dream] `model`, else the built-in default qwen3.5:9b; runs
#      only after an explicit [y/N] confirmation; --yes skips the prompt;
#      a model is NEVER pulled without that confirmation)
#   7. OpenCode host adapter hook (mnemoseed-local hook install opencode)
#   8. final mnemoseed-local doctor re-check + next steps
#      (mnemoseed-local up; hook already installed)
#
# Idempotent: every step skips when already satisfied. Every failed external
# install operation prints a one-line reason to stderr and exits non-zero.
# Doctor verdicts are readiness reports, not install operations: a failed
# check is noted on stderr but never aborts the orchestration (on a fresh
# machine the model check fails until step 6 pulls the model).
#
# --tier is a convenience hint only: it makes the tier-adjust hint explicit
# without ever changing config keys.
#
# --dry-run prints the numbered plan with command-existence probe results and
# performs ZERO side effects (no installers, no init, no doctor, no pull).

set -eu

: "${HOME:?HOME is not set}"

CONFIG_HOME="${MNEMOSEED_LOCAL_HOME:-$HOME/.mnemoseed-local}"
CONFIG_PATH="$CONFIG_HOME/config.toml"
UV_BIN_DIR="$HOME/.local/bin"
DEFAULT_MODEL='qwen3.5:9b'
OLLAMA_URL='https://ollama.com/install.sh'
UV_URL='https://astral.sh/uv/install.sh'

DRY_RUN=0
YES=0
TIER=""

die() {
    printf '%s\n' "install.sh: error: $*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
install.sh - MnemoSeed Local zero-dependency install orchestrator (Linux/macOS, POSIX sh)

usage:
  curl -fsSL https://raw.githubusercontent.com/MnemoSeed/mnemoseed-local/main/install.sh | sh
  curl -fsSL .../install.sh | sh -s -- [--dry-run] [--yes] [--tier lite|standard|advanced]
  sh ./install.sh [--dry-run] [--yes] [--tier lite|standard|advanced]

options:
  --dry-run        print the numbered plan with probe results; zero side effects
  --yes, -y        skip the [y/N] confirmation before `ollama pull`
  --tier <tier>    lite | standard | advanced - prints an explicit hint for
                   `config set dream.hardware_tier <tier>` (never changes config itself)
  -h, --help       show this help

orchestration: ollama -> uv -> uv tool -> init -> doctor -> confirm+pull -> hook -> doctor
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --yes|-y) YES=1 ;;
        --tier)
            [ $# -ge 2 ] || die "--tier requires a value (lite|standard|advanced)"
            TIER="$2"
            shift
            ;;
        --tier=*) TIER="${1#--tier=}" ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
    shift
done
case "$TIER" in
    ''|lite|standard|advanced) ;;
    *) die "unknown tier '$TIER' (expected lite|standard|advanced)" ;;
esac

have() { command -v "$1" >/dev/null 2>&1; }

# fetch_pipe URL -> stream URL to stdout with curl (preferred) or wget.
fetch_pipe() {
    if have curl; then
        curl -fsSL "$1"
    elif have wget; then
        wget -qO- "$1"
    else
        die "neither curl nor wget is available; install one of them to download $1"
    fi
}

# prepend_path DIR -> add DIR to PATH for this process when it exists and is not present.
prepend_path() {
    [ -d "$1" ] || return 0
    case ":$PATH:" in
        *":$1:"*) return 0 ;;
    esac
    PATH="$1:$PATH"
    export PATH
    printf '%s\n' "      added $1 to the current process PATH"
}

OS_NAME="$(uname -s 2>/dev/null || printf 'unknown')"

# resolve_model PATH -> the dream route's model, from the source doctor/up
# check against ("pull what will be checked"): the ACTIVE [dream.llm.dream]
# table's `model = "..."` key. Commented template lines are ignored; fall back
# to the built-in default when the key (or the file) is absent.
resolve_model() {
    _cfg="$1"
    if [ ! -f "$_cfg" ]; then
        printf '%s\n' "$DEFAULT_MODEL"
        return 0
    fi
    _m="$(awk '
        /^[[:blank:]]*#/ { next }
        /^[[:blank:]]*\[[^]]+\]/ {
            h = $0
            sub(/^[[:blank:]]*\[/, "", h)
            sub(/\].*$/, "", h)
            gsub(/[[:blank:]]/, "", h)
            in_dream = (h == "dream.llm.dream")
            next
        }
        in_dream && /^[[:blank:]]*model[[:blank:]]*=/ {
            v = $0
            sub(/^[^=]*=/, "", v)
            sub(/^[[:blank:]]+/, "", v)
            c = substr(v, 1, 1)
            sq = sprintf("%c", 39)
            if (c == "\"" || c == sq) {
                sub(/^./, "", v)
                i = index(v, c)
                if (i > 1) { print substr(v, 1, i - 1) }
            }
            exit
        }
    ' "$_cfg")"
    if [ -n "$_m" ]; then
        printf '%s\n' "$_m"
    else
        printf '%s\n' "$DEFAULT_MODEL"
    fi
}

# extract_tier WHICH TEXT -> the tier token out of the doctor hardware-tier
# detail line (pinned contract, emitted by the CLI: `recommended tier
# "standard" (vram=12GB, ram=32GB); current tier "standard"`).
extract_tier() {
    printf '%s\n' "$2" | sed -n "s/.*$1 tier \"\([A-Za-z0-9_]*\)\".*/\1/p" | head -n 1
}

show_tier_hints() {
    _rec="$(extract_tier recommended "$1")"
    _cur="$(extract_tier current "$1")"
    if [ -n "$_rec" ] && [ -n "$_cur" ] && [ "$_rec" != "$_cur" ]; then
        printf '%s\n' "      hint: doctor recommends hardware tier \"$_rec\" but the current tier is \"$_cur\". This installer"
        printf '%s\n' "            never changes config keys; you may adjust them yourself with:"
        printf '%s\n' "              mnemoseed-local config set dream.hardware_tier $_rec"
        printf '%s\n' "            and the matching model under dream.llm.dream.model ([dream.llm.dream] table, key \"model\")."
    fi
    if [ -n "$TIER" ]; then
        printf '%s\n' "      hint: --tier $TIER was requested; this installer never changes config keys. To apply it, run:"
        printf '%s\n' "              mnemoseed-local config set dream.hardware_tier $TIER"
    fi
}

# run_doctor -> echo doctor output verbatim; set DOCTOR_RC and DOCTOR_OUT.
# The doctor exit code is a readiness report, so callers decide what it means.
run_doctor() {
    if DOCTOR_OUT="$(mnemoseed-local doctor 2>&1)"; then
        DOCTOR_RC=0
    else
        DOCTOR_RC=$?
    fi
    printf '%s\n' "$DOCTOR_OUT"
}

# --- dry-run plan -----------------------------------------------------------

if [ "$DRY_RUN" -eq 1 ]; then
    printf '%s\n' 'mnemoseed-local install plan (DRY-RUN: zero side effects; probes are command-existence checks only)'
    printf '\n'
    printf '%s\n' '[1] ollama'
    if have ollama; then
        printf '%s\n' "    probe: ollama command FOUND at $(command -v ollama)"
        printf '%s\n' '    plan:  skip install (already present)'
    else
        printf '%s\n' '    probe: ollama command NOT FOUND'
        case "$OS_NAME" in
            Linux*)
                printf '%s\n' "    plan:  install via the official script ($OLLAMA_URL | sh)"
                ;;
            Darwin*)
                printf '%s\n' '    plan:  macOS - would print install hints (brew install ollama / https://ollama.com/download/mac) and exit non-zero'
                ;;
            *)
                printf '%s\n' "    plan:  unsupported OS ($OS_NAME) for the official script - would print a manual hint (https://ollama.com/download) and exit non-zero"
                ;;
        esac
    fi
    printf '%s\n' '[2] uv'
    if have uv; then
        printf '%s\n' "    probe: uv command FOUND at $(command -v uv)"
        printf '%s\n' "    plan:  skip install; prepend $UV_BIN_DIR to the current process PATH when present"
    else
        printf '%s\n' '    probe: uv command NOT FOUND'
        if have curl || have wget; then
            printf '%s\n' "    plan:  install via the official installer ($UV_URL | sh), then prepend $UV_BIN_DIR to the current process PATH"
        else
            printf '%s\n' '    plan:  neither curl nor wget FOUND - would fail with a downloader hint and exit non-zero'
        fi
    fi
    printf '%s\n' '[3] mnemoseed-local CLI'
    if have mnemoseed-local; then
        printf '%s\n' "    probe: mnemoseed-local command FOUND at $(command -v mnemoseed-local)"
        printf '%s\n' '    plan:  would run `uv tool upgrade mnemoseed-local`'
    else
        printf '%s\n' '    probe: mnemoseed-local command NOT FOUND'
        printf '%s\n' '    plan:  would run `uv tool install mnemoseed-local`'
    fi
    printf '%s\n' '[4] init'
    printf '%s\n' "    plan:  would run \`mnemoseed-local init\` when $CONFIG_PATH does not exist; skipped when present"
    printf '%s\n' '[5] doctor (first pass)'
    printf '%s\n' '    plan:  would run `mnemoseed-local doctor` (output shown verbatim), then compare the hardware-tier detail'
    printf '%s\n' '           (`recommended tier "<tier>"` vs `current tier "<tier>"`; a hint is printed when they differ; config is never changed)'
    if [ -n "$TIER" ]; then
        printf '%s\n' "    plan:  --tier $TIER given - would print the hint-only \`config set dream.hardware_tier $TIER\` instruction"
    fi
    printf '%s\n' '[6] model pull (requires confirmation)'
    printf '%s\n' "    plan:  would resolve the dream model from $CONFIG_PATH (an ACTIVE [dream.llm.dream] \`model\` key; default $DEFAULT_MODEL),"
    printf '%s\n' '           prompt [y/N] (skipped by --yes), then run `ollama pull <model>` - NEVER without that confirmation'
    printf '%s\n' '[7] OpenCode host adapter hook'
    printf '%s\n' '    plan:  would run `mnemoseed-local hook install opencode`'
    printf '%s\n' '[8] final doctor + guidance'
    printf '%s\n' '    plan:  would re-run `mnemoseed-local doctor` verbatim, then print next steps'
    printf '%s\n' '           (`mnemoseed-local up`; hook already installed)'
    printf '\n'
    printf '%s\n' 'dry-run complete: no installers ran, no init, no doctor, no pull - nothing changed'
    exit 0
fi

# --- step 1: ollama ---------------------------------------------------------

printf '%s\n' '[1/8] ollama'
if have ollama; then
    printf '%s\n' '      found - skipping install'
else
    case "$OS_NAME" in
        Linux*)
            printf '%s\n' '      not found - installing via the official script...'
            if ! fetch_pipe "$OLLAMA_URL" | sh; then
                die "the official ollama installer failed; install it manually from https://ollama.com/download"
            fi
            have ollama || die "ollama is still not on PATH after the install; see https://ollama.com/download"
            printf '%s\n' '      installed'
            ;;
        Darwin*)
            die "ollama not found; on macOS install it with 'brew install ollama' or from https://ollama.com/download/mac, then re-run this script"
            ;;
        *)
            die "ollama not found and this platform ($OS_NAME) has no script installer; install ollama manually from https://ollama.com/download, then re-run this script"
            ;;
    esac
fi

# --- step 2: uv -------------------------------------------------------------

printf '%s\n' '[2/8] uv'
if have uv; then
    printf '%s\n' '      found - skipping install'
else
    printf '%s\n' '      not found - installing via the official installer...'
    if ! fetch_pipe "$UV_URL" | sh; then
        die "the official uv installer failed; see https://docs.astral.sh/uv/getting-started/installation/"
    fi
    prepend_path "$UV_BIN_DIR"
    have uv || die "uv is still not on PATH after the install; add $UV_BIN_DIR to PATH and re-run"
    printf '%s\n' '      installed'
fi
prepend_path "$UV_BIN_DIR"

# --- step 3: the mnemoseed-local CLI ---------------------------------------

printf '%s\n' '[3/8] mnemoseed-local CLI'
if have mnemoseed-local; then
    printf '%s\n' '      found - upgrading via uv tool...'
    if ! uv tool upgrade mnemoseed-local; then
        die "'uv tool upgrade mnemoseed-local' failed"
    fi
else
    printf '%s\n' '      not found - installing via uv tool...'
    if ! uv tool install mnemoseed-local; then
        die "'uv tool install mnemoseed-local' failed"
    fi
fi
have mnemoseed-local || die "mnemoseed-local was installed but is not on PATH; add $UV_BIN_DIR to PATH and re-run"

# --- step 4: init ------------------------------------------------------------

printf '%s\n' '[4/8] init'
if [ -f "$CONFIG_PATH" ]; then
    printf '%s\n' "      $CONFIG_PATH already exists - skipping"
else
    if ! mnemoseed-local init; then
        die "'mnemoseed-local init' failed"
    fi
fi

# --- step 5: doctor (first pass) + hardware-tier hint ------------------------

printf '%s\n' '[5/8] doctor (first pass)'
run_doctor
if [ "$DOCTOR_RC" -ne 0 ]; then
    printf '%s\n' 'install.sh: note: doctor reported failures (expected before the model pull) - continuing' >&2
fi
show_tier_hints "$DOCTOR_OUT"

# --- step 6: confirmation-gated model pull -----------------------------------

printf '%s\n' '[6/8] dream model pull'
MODEL="$(resolve_model "$CONFIG_PATH")"
if [ "$MODEL" = "$DEFAULT_MODEL" ]; then
    printf '%s\n' "      model to pull: $MODEL (built-in default - the dream route's check target)"
else
    printf '%s\n' "      model to pull: $MODEL (from $CONFIG_PATH [dream.llm.dream] \`model\` - the dream route's check target)"
fi
PULL_CONFIRMED=0
if [ "$YES" -eq 1 ]; then
    PULL_CONFIRMED=1
    printf '%s\n' '      --yes given - skipping the confirmation prompt'
else
    printf '%s' "      pull it now with \`ollama pull $MODEL\`? [y/N] "
    ANSWER=""
    if [ -t 0 ]; then
        read -r ANSWER || ANSWER=""
    elif [ -r /dev/tty ]; then
        read -r ANSWER < /dev/tty || ANSWER=""
    fi
    case "$ANSWER" in
        y|Y|yes|YES|Yes) PULL_CONFIRMED=1 ;;
    esac
fi
if [ "$PULL_CONFIRMED" -eq 0 ]; then
    printf '%s\n' "      skipped (no confirmation); pull it later with: ollama pull $MODEL"
else
    if ! ollama pull "$MODEL"; then
        die "'ollama pull $MODEL' failed; is the ollama server running? retry with: ollama pull $MODEL"
    fi
    printf '%s\n' '      pulled'
fi

# --- step 7: hook install ------------------------------------------------------

printf '%s\n' '[7/8] OpenCode host adapter hook'
if ! mnemoseed-local hook install opencode; then
    printf '%s\n' "install.sh: note: 'mnemoseed-local hook install opencode' failed; you can run it manually later" >&2
else
    printf '%s\n' '      installed'
fi

# --- step 8: final doctor + guidance -----------------------------------------

printf '%s\n' '[8/8] doctor (final re-check)'
run_doctor
if [ "$DOCTOR_RC" -ne 0 ]; then
    printf '%s\n' 'install.sh: note: doctor still reports failures; resolve them, then re-run `mnemoseed-local doctor`' >&2
fi
printf '\n'
printf '%s\n' 'installation complete.'
printf '%s\n' 'next steps:'
printf '%s\n' '  mnemoseed-local up            # start the daemon'
printf '%s\n' '  (hook already installed; register MCP gateway in opencode.json if needed)'
exit 0
