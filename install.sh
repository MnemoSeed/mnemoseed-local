#!/bin/sh
# install.sh - MnemoSeed Local zero-dependency install orchestrator (Linux/macOS, POSIX sh).
#
# One-line entry:
#   curl -fsSL https://raw.githubusercontent.com/MnemoSeed/mnemoseed-local/main/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- [--dry-run] [--yes] [--skip-ollama] [--tier lite|standard|advanced] [--mode capture-recall|refinement] [--hosts <csv>]
# Or run as a file:
#   sh ./install.sh [--dry-run] [--yes] [--skip-ollama] [--tier <lite|standard|advanced>] [--mode <capture-recall|refinement>] [--hosts <csv>]
#
# Orchestration order (identical to install.ps1):
#   1. detect / install ollama   (skipped in capture-recall mode: that mode runs
#      capture, local embeddings and recall without Ollama; Linux: official
#      install script; macOS: `brew install ollama` behind an explicit [y/N]
#      consent (--yes grants it), else the exact manual commands and a non-zero
#      exit; --skip-ollama skips the setup and marks the dream route
#      unavailable; other platforms: print a manual hint and exit non-zero)
#   2. detect / install uv       (official installer; well-known install dir is
#      prepended to the current process PATH)
#   3. install / upgrade the CLI (uv tool install | uv tool upgrade, both pinned
#      to the exact mnemoseed-local[codex]==<version> spec; never a floating latest)
#   4. mnemoseed-local init --mode <mode>   (skipped when ~/.mnemoseed-local/config.toml exists)
#   5. mnemoseed-local doctor    (verbatim) + hardware-tier hint; hint-only,
#      the script never changes config keys itself
#   6. ollama pull <dream model> (skipped in capture-recall mode and by
#      --skip-ollama: the legacy auto dream stays off there; otherwise the
#      dream route's model from config.toml [dream.llm.dream] `model`, else
#      the built-in default qwen3.5:9b; runs only after an explicit [y/N]
#      confirmation; --yes skips the prompt; a model is NEVER pulled without
#      that confirmation)
#   7. host adapter wiring (ONLY the hosts named in --hosts <csv>, or the
#      hosts picked from an interactive offer of the detected ones: a host
#      counts as installed when its CLI is on PATH or its adapter config
#      directory exists; the offer runs only when stdin is a terminal, a
#      non-interactive run without --hosts wires NOTHING and reports what it
#      detected next to the exact per-host commands in the next steps; an
#      unknown --hosts value refuses before anything is written)
#   8. final mnemoseed-local doctor re-check + next steps
#      (mnemoseed-local up; the selected hosts are wired, or the per-host
#      commands are printed when none were selected)
#
# --mode selects the install-time setup posture passed through to
# `mnemoseed-local init --mode <mode>`, which maps it onto the two existing
# config keys (dream.auto_trigger / capture.auto_recall); no new keys, and the
# setup never rewrites an existing config. The default is capture-recall: the
# first-trial setup runs capture, local embeddings and recall without Ollama.
# refinement prints that v2 is a design proposal only and never re-enables the
# legacy auto dream.
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
# The exact wheel spec: a floating `latest` would silently track main. Keep in
# lockstep with pyproject.toml (tests/test_version_single_source.py enforces it).
CLI_PIN='mnemoseed-local[codex]==0.2.0'
OLLAMA_URL='https://ollama.com/install.sh'
UV_URL='https://astral.sh/uv/install.sh'

DRY_RUN=0
YES=0
SKIP_OLLAMA=0
TIER=""
MODE='capture-recall'
HOSTS=''

die() {
    printf '%s\n' "install.sh: error: $*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
install.sh - MnemoSeed Local zero-dependency install orchestrator (Linux/macOS, POSIX sh)

usage:
  curl -fsSL https://raw.githubusercontent.com/MnemoSeed/mnemoseed-local/main/install.sh | sh
  curl -fsSL .../install.sh | sh -s -- [--dry-run] [--yes] [--skip-ollama] [--tier lite|standard|advanced] [--mode capture-recall|refinement] [--hosts <csv>]
  sh ./install.sh [--dry-run] [--yes] [--skip-ollama] [--tier lite|standard|advanced] [--mode capture-recall|refinement] [--hosts <csv>]

options:
  --dry-run        print the numbered plan with probe results; zero side effects
  --yes, -y        skip the [y/N] confirmations (the Homebrew Ollama install on
                   macOS and the `ollama pull` prompt)
  --skip-ollama    skip the Ollama setup: no install attempt and no model pull;
                   when Ollama is missing the install finishes and
                   the dream route is marked unavailable (the exact re-enable
                   steps are printed at run time)
  --tier <tier>    lite | standard | advanced - prints an explicit hint for
                   `config set dream.hardware_tier <tier>` (never changes config itself)
  --mode <mode>    capture-recall (default) | refinement - the setup posture passed
                   to `mnemoseed-local init --mode <mode>`; an existing config is
                   never rewritten
  --hosts <csv>    comma-separated host adapters to wire, from:
                   claude_code | codex_cli | opencode | zcode
                   (default: none - an interactive session is offered a
                   selection of the detected hosts, a non-interactive run
                   wires nothing and prints the exact per-host commands under
                   'next steps'; 'all' is not accepted - name the hosts
                   explicitly)
  -h, --help       show this help

orchestration: ollama -> uv -> uv tool -> init -> doctor -> confirm+pull -> hosts -> doctor
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --yes|-y) YES=1 ;;
        --skip-ollama) SKIP_OLLAMA=1 ;;
        --tier)
            [ $# -ge 2 ] || die "--tier requires a value (lite|standard|advanced)"
            TIER="$2"
            shift
            ;;
        --tier=*) TIER="${1#--tier=}" ;;
        --mode)
            [ $# -ge 2 ] || die "--mode requires a value (capture-recall|refinement)"
            MODE="$2"
            shift
            ;;
        --mode=*) MODE="${1#--mode=}" ;;
        --hosts)
            [ $# -ge 2 ] || die "--hosts requires a value (claude_code|codex_cli|opencode|zcode)"
            HOSTS="$2"
            shift
            ;;
        --hosts=*) HOSTS="${1#--hosts=}" ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
    shift
done
case "$TIER" in
    ''|lite|standard|advanced) ;;
    *) die "unknown tier '$TIER' (expected lite|standard|advanced)" ;;
esac
case "$MODE" in
    capture-recall|refinement) ;;
    *) die "unknown mode '$MODE' (expected capture-recall|refinement)" ;;
esac

# validate_host_list CSV -> the deduplicated adapter keys, one per line. Every
# token must be an exact adapter key; anything else refuses. 'all' is rejected
# on purpose: silently wiring every host is exactly what --hosts must never do.
validate_host_list() {
    _rest="$1,"
    _seen="|"
    while [ -n "$_rest" ]; do
        _key="${_rest%%,*}"
        _rest="${_rest#*,}"
        case "$_key" in
            claude_code|codex_cli|opencode|zcode) ;;
            *) die "unknown host '$_key' (expected claude_code|codex_cli|opencode|zcode; 'all' is not accepted - name the hosts explicitly)" ;;
        esac
        case "$_seen" in
            *"|$_key|"*) continue ;;
        esac
        _seen="$_seen$_key|"
        printf '%s\n' "$_key"
    done
}

# Host wiring is explicit-only (--hosts) or an interactive offer over the
# detected hosts; the selection is fully validated BEFORE any step runs (a
# refusal must precede every write; the die inside the substitution aborts
# under set -e).
HOST_SELECTED=""
HOST_DETECTED=""
if [ -n "$HOSTS" ]; then
    HOST_SELECTED="$(validate_host_list "$HOSTS")"
fi

have() { command -v "$1" >/dev/null 2>&1; }

# host_config_dir KEY -> success when the host adapter's config directory
# exists. The resolution mirrors each adapter's own install target (env
# override first, then the well-known default), so detection agrees with what
# `host install` would write.
host_config_dir() {
    case "$1" in
        claude_code)
            if [ -d "$HOME/.claude" ]; then return 0; fi
            ;;
        codex_cli)
            if [ -n "${CODEX_HOME:-}" ]; then
                if [ -d "$CODEX_HOME" ]; then return 0; fi
            else
                if [ -d "$HOME/.codex" ]; then return 0; fi
            fi
            ;;
        opencode)
            if [ -n "${OPENCODE_CONFIG_DIR:-}" ]; then
                if [ -d "$OPENCODE_CONFIG_DIR" ]; then return 0; fi
            elif [ -n "${XDG_CONFIG_HOME:-}" ]; then
                if [ -d "$XDG_CONFIG_HOME/opencode" ]; then return 0; fi
            else
                if [ -d "$HOME/.config/opencode" ]; then return 0; fi
            fi
            ;;
        zcode)
            if [ -d "$HOME/.zcode" ]; then return 0; fi
            ;;
    esac
    return 1
}

# detect_installed_hosts -> the installed host keys, space-separated on one
# line in registry order: a host counts as installed when its CLI is on PATH
# or its adapter config directory exists.
detect_installed_hosts() {
    _detected=""
    for _pair in claude_code:claude codex_cli:codex opencode:opencode zcode:zcode; do
        _key="${_pair%%:*}"
        _cli="${_pair#*:}"
        if have "$_cli" || host_config_dir "$_key"; then
            if [ -n "$_detected" ]; then
                _detected="$_detected $_key"
            else
                _detected="$_key"
            fi
        fi
    done
    printf '%s\n' "$_detected"
}

# menu_select_hosts ANSWER KEY... -> the chosen keys, one per line in answer
# order (deduplicated): only a menu number selects a host, so any other token
# can never wire a host that was not offered.
menu_select_hosts() {
    _answer="$1"
    shift
    _seen="|"
    _rest="$_answer,"
    while [ -n "$_rest" ]; do
        _token="${_rest%%,*}"
        _rest="${_rest#*,}"
        [ -n "$_token" ] || continue
        case "$_token" in
            *[!0-9]*) continue ;;
            0) continue ;;
        esac
        _i=1
        _key=""
        for _candidate in "$@"; do
            if [ "$_i" -eq "$_token" ]; then
                _key="$_candidate"
                break
            fi
            _i=$((_i + 1))
        done
        [ -n "$_key" ] || continue
        case "$_seen" in
            *"|$_key|"*) continue ;;
        esac
        _seen="$_seen$_key|"
        printf '%s\n' "$_key"
    done
}

# offer_installed_host_selection KEY... -> print the numbered menu to stderr,
# read the answer from stdin (the caller gates on stdin being a terminal) and
# echo only the chosen keys to stdout, so a command-substitution capture sees
# the selection alone; an empty or unparsable answer selects nothing.
offer_installed_host_selection() {
    printf '%s\n' '      detected installed hosts:' >&2
    _n=1
    for _host in "$@"; do
        printf '        %s) %s\n' "$_n" "$_host" >&2
        _n=$((_n + 1))
    done
    _answer=""
    printf '%s' '      wire which hosts? comma-separated numbers, empty to skip: ' >&2
    read -r _answer || _answer=""
    _answer="$(printf '%s' "$_answer" | tr -s ' \t' ',,')"
    menu_select_hosts "$_answer" "$@"
}

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

# confirm PROMPT -> 0 only on an explicit yes: the prompt prints to stdout and
# the answer is read from a terminal stdin or /dev/tty, never from a piped
# stdin, because a `curl | sh` run streams the script itself on stdin.
confirm() {
    printf '%s' "$1"
    _answer=""
    if [ -t 0 ]; then
        read -r _answer || _answer=""
    elif [ -r /dev/tty ]; then
        read -r _answer < /dev/tty || _answer=""
    fi
    case "$_answer" in
        y|Y|yes|YES|Yes) return 0 ;;
    esac
    return 1
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

# plan_ollama -> the dry-run text of the step-1 Ollama plan for the selected
# mode, flag and OS; every probe is a command-existence check only.
plan_ollama() {
    if [ "$MODE" = "capture-recall" ]; then
        printf '%s\n' '    plan:  skipped (mode capture-recall runs capture, local embeddings and recall without Ollama)'
        return 0
    fi
    if have ollama; then
        printf '%s\n' "    probe: ollama command FOUND at $(command -v ollama)"
        printf '%s\n' '    plan:  skip install (already present)'
        return 0
    fi
    printf '%s\n' '    probe: ollama command NOT FOUND'
    if [ "$SKIP_OLLAMA" -eq 1 ]; then
        printf '%s\n' '    plan:  skipped (--skip-ollama): no Ollama install attempt and no model pull;'
        printf '%s\n' '           the install finishes and the dream route is marked unavailable when Ollama is absent'
        return 0
    fi
    case "$OS_NAME" in
        Linux*)
            printf '%s\n' "    plan:  install via the official script ($OLLAMA_URL | sh)"
            ;;
        Darwin*)
            if have brew; then
                printf '%s\n' "    probe: brew command FOUND at $(command -v brew)"
                printf '%s\n' '    plan:  would ask [y/N] consent, then install via `brew install ollama`'
                printf '%s\n' '           (--yes grants the consent; declining exits non-zero with the exact manual commands)'
            else
                printf '%s\n' '    probe: brew command NOT FOUND'
                printf '%s\n' '    plan:  would exit non-zero with the exact manual steps: install Homebrew'
                printf '%s\n' '           (https://brew.sh) or Ollama (https://ollama.com/download/mac), or re-run with --skip-ollama'
            fi
            ;;
        *)
            printf '%s\n' "    plan:  unsupported OS ($OS_NAME) for the official script - would print a manual hint (https://ollama.com/download) and exit non-zero"
            ;;
    esac
}

# install_ollama -> the step-1 Ollama setup: skipped by mode, skipped when
# present, skipped by --skip-ollama, installed on Linux via the official
# script, installed on macOS via Homebrew behind an explicit consent, or a
# refusal printing the exact manual commands (that refusal exits non-zero;
# --skip-ollama finishes with the dream route marked unavailable).
install_ollama() {
    if [ "$MODE" = "capture-recall" ]; then
        printf '%s\n' '      skipped (mode capture-recall runs capture, local embeddings and recall without Ollama)'
        printf '%s\n' '      to use the dream engine later: install Ollama (https://ollama.com/download), then re-run `mnemoseed-local doctor`'
        return 0
    fi
    if have ollama; then
        printf '%s\n' '      found - skipping install'
        return 0
    fi
    if [ "$SKIP_OLLAMA" -eq 1 ]; then
        printf '%s\n' '      skipped (--skip-ollama): ollama is not on PATH and no Ollama install was attempted'
        printf '%s\n' '      the dream route is unavailable for this install. To enable it later:'
        printf '%s\n' '        install Ollama (macOS: brew install ollama; Linux: re-run install.sh without --skip-ollama;'
        printf '%s\n' '        manual: https://ollama.com/download), then re-run `mnemoseed-local doctor`'
        return 0
    fi
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
            if have brew; then
                printf '%s\n' '      not found - Homebrew is available'
                if [ "$YES" -eq 1 ]; then
                    printf '%s\n' '      --yes given - skipping the confirmation prompt'
                elif ! confirm '      install Ollama now with Homebrew (`brew install ollama`)? [y/N] '; then
                    die "no Ollama install consent; re-run with --skip-ollama to finish the install without the dream route, or install Ollama yourself with 'brew install ollama' or from https://ollama.com/download/mac and re-run this script"
                fi
                printf '%s\n' '      installing Ollama via Homebrew...'
                if ! brew install ollama; then
                    die "'brew install ollama' failed; install it manually from https://ollama.com/download/mac and re-run this script"
                fi
                have ollama || die "ollama is still not on PATH after the brew install; open a new shell or install it from https://ollama.com/download/mac and re-run this script"
                printf '%s\n' '      installed'
            else
                die "ollama not found and Homebrew is not installed; install Homebrew from https://brew.sh and re-run this script, or install Ollama from https://ollama.com/download/mac, or re-run with --skip-ollama to finish the install without the dream route"
            fi
            ;;
        *)
            die "ollama not found and this platform ($OS_NAME) has no script installer; install ollama manually from https://ollama.com/download, or re-run with --skip-ollama to finish the install without the dream route"
            ;;
    esac
}

# --- dry-run plan -----------------------------------------------------------

if [ "$DRY_RUN" -eq 1 ]; then
    printf '%s\n' 'mnemoseed-local install plan (DRY-RUN: zero side effects; probes are command-existence checks only)'
    printf '\n'
    printf '%s\n' '[1] ollama'
    plan_ollama
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
        printf '%s\n' "    plan:  would run \`uv tool upgrade --python 3.12 $CLI_PIN\`"
    else
        printf '%s\n' '    probe: mnemoseed-local command NOT FOUND'
        printf '%s\n' "    plan:  would run \`uv tool install --python 3.12 $CLI_PIN\`"
    fi
    printf '%s\n' '[4] init'
    printf '%s\n' "    plan:  would run \`mnemoseed-local init --mode $MODE\` when $CONFIG_PATH does not exist; skipped when present"
    printf '%s\n' '[5] doctor (first pass)'
    printf '%s\n' '    plan:  would run `mnemoseed-local doctor` (output shown verbatim), then compare the hardware-tier detail'
    printf '%s\n' '           (`recommended tier "<tier>"` vs `current tier "<tier>"`; a hint is printed when they differ; config is never changed)'
    if [ -n "$TIER" ]; then
        printf '%s\n' "    plan:  --tier $TIER given - would print the hint-only \`config set dream.hardware_tier $TIER\` instruction"
    fi
    printf '%s\n' '[6] model pull (requires confirmation)'
    if [ "$MODE" = "capture-recall" ]; then
        printf '%s\n' '    plan:  skipped (mode capture-recall keeps the legacy auto dream off; no dream model is pulled)'
    elif [ "$SKIP_OLLAMA" -eq 1 ]; then
        printf '%s\n' '    plan:  skipped (--skip-ollama): no dream model pull is attempted'
    else
        printf '%s\n' "    plan:  would resolve the dream model from $CONFIG_PATH (an ACTIVE [dream.llm.dream] \`model\` key; default $DEFAULT_MODEL),"
        printf '%s\n' '           prompt [y/N] (skipped by --yes), then run `ollama pull <model>` - NEVER without that confirmation'
    fi
    printf '%s\n' '[7] host adapter wiring (explicit --hosts or an interactive offer; never automatic)'
    if [ -z "$HOST_SELECTED" ]; then
        HOST_PROBED="$(detect_installed_hosts)"
        if [ -n "$HOST_PROBED" ]; then
            printf '%s\n' "    probe: detected installed hosts: $HOST_PROBED"
        else
            printf '%s\n' '    probe: no installed host detected (host CLIs on PATH or their config dirs)'
        fi
        printf '%s\n' '    plan:  no host adapter wiring (no --hosts value; the daemon-only install stays fully functional)'
        printf '%s\n' '    plan:  an interactive session would be offered a selection of the detected hosts'
        printf '%s\n' '    plan:  the exact per-host commands would be printed under next steps'
    else
        for _host in $HOST_SELECTED; do
            case "$_host" in
                opencode) printf '%s\n' '    plan:  would run `mnemoseed-local hook install opencode`' ;;
                *) printf '%s\n' "    plan:  would run \`mnemoseed-local host install --host $_host\`" ;;
            esac
        done
    fi
    printf '%s\n' '[8] final doctor + guidance'
    printf '%s\n' '    plan:  would re-run `mnemoseed-local doctor` verbatim, then print next steps'
    printf '%s\n' '           (mnemoseed-local up; the selected hosts are wired, or the exact per-host'
    printf '%s\n' '           commands are printed when no --hosts value was given)'
    printf '\n'
    printf '%s\n' 'dry-run complete: no installers ran, no init, no doctor, no pull - nothing changed'
    exit 0
fi

# --- step 1: ollama ---------------------------------------------------------

printf '%s\n' '[1/8] ollama'
install_ollama

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
    printf '%s\n' "      found - upgrading via uv tool ($CLI_PIN)..."
    if ! uv tool upgrade --python 3.12 "$CLI_PIN"; then
        die "'uv tool upgrade --python 3.12 $CLI_PIN' failed"
    fi
else
    printf '%s\n' "      not found - installing via uv tool ($CLI_PIN)..."
    if ! uv tool install --python 3.12 "$CLI_PIN"; then
        die "'uv tool install --python 3.12 $CLI_PIN' failed"
    fi
fi
have mnemoseed-local || die "mnemoseed-local was installed but is not on PATH; add $UV_BIN_DIR to PATH and re-run"

# --- step 4: init ------------------------------------------------------------

printf '%s\n' '[4/8] init'
if [ -f "$CONFIG_PATH" ]; then
    printf '%s\n' "      $CONFIG_PATH already exists - skipping"
    printf '%s\n' "      to inspect or convert its setup mode: mnemoseed-local init --mode $MODE"
else
    if ! mnemoseed-local init --mode "$MODE"; then
        die "'mnemoseed-local init --mode $MODE' failed"
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
if [ "$MODE" = "capture-recall" ]; then
    printf '%s\n' '      skipped (mode capture-recall keeps the legacy auto dream off; no dream model is pulled)'
    printf '%s\n' '      for manual consolidation later: install Ollama, then `ollama pull <dream model>` (see `mnemoseed-local doctor`)'
elif [ "$SKIP_OLLAMA" -eq 1 ]; then
    printf '%s\n' '      skipped (--skip-ollama): no dream model pull was attempted'
    if have ollama; then
        printf '%s\n' '      ollama is on PATH; pull a dream model later with: ollama pull <dream model> (see `mnemoseed-local doctor`)'
    else
        printf '%s\n' '      ollama is not on PATH: the dream route is unavailable for this install'
        printf '%s\n' '      to enable it later: install Ollama (https://ollama.com/download), then `ollama pull <dream model>` (see `mnemoseed-local doctor`)'
    fi
else
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
    elif confirm "      pull it now with \`ollama pull $MODEL\`? [y/N] "; then
        PULL_CONFIRMED=1
    fi
    if [ "$PULL_CONFIRMED" -eq 0 ]; then
        printf '%s\n' "      skipped (no confirmation); pull it later with: ollama pull $MODEL"
    else
        if ! ollama pull "$MODEL"; then
            die "'ollama pull $MODEL' failed; is the ollama server running? retry with: ollama pull $MODEL"
        fi
        printf '%s\n' '      pulled'
    fi
fi

# --- step 7: host adapter wiring ---------------------------------------------

printf '%s\n' '[7/8] host adapter wiring'
if [ -z "$HOST_SELECTED" ]; then
    HOST_DETECTED="$(detect_installed_hosts)"
    # The offer reads its answer from stdin, so it can run only when stdin is
    # a terminal: a `curl ... | sh` pipe must never lose script bytes to an
    # answer read, and a non-interactive session is never prompted.
    if [ -n "$HOST_DETECTED" ] && [ -t 0 ]; then
        HOST_SELECTED="$(offer_installed_host_selection $HOST_DETECTED)"
    fi
fi
if [ -z "$HOST_SELECTED" ]; then
    if [ -n "$HOST_DETECTED" ]; then
        if [ -t 0 ]; then
            printf '%s\n' '      nothing selected: no host adapter was wired; re-run with --hosts <csv> or use the commands under next steps'
        else
            printf '%s\n' "      detected installed hosts: $HOST_DETECTED"
            printf '%s\n' '      non-interactive session: nothing was wired; re-run with --hosts <csv> or use the commands under next steps'
        fi
    else
        printf '%s\n' '      detection: no installed host found (host CLIs on PATH or their config dirs)'
    fi
    printf '%s\n' '      skipped: no --hosts value given - no host adapter was wired'
    printf '%s\n' '      the daemon install above is fully functional without a host adapter;'
    printf '%s\n' "      wire hosts later with the exact commands under 'next steps' (or re-run with --hosts <csv>)"
else
    for _host in $HOST_SELECTED; do
        case "$_host" in
            opencode)
                printf '%s\n' '      host opencode: running: mnemoseed-local hook install opencode'
                if ! mnemoseed-local hook install opencode; then
                    printf '%s\n' "install.sh: note: the opencode adapter install failed (installation continues); retry manually with: mnemoseed-local hook install opencode" >&2
                fi
                ;;
            *)
                printf '%s\n' "      host $_host: running: mnemoseed-local host install --host $_host"
                if ! mnemoseed-local host install --host "$_host"; then
                    printf '%s\n' "install.sh: note: the $_host adapter install failed (installation continues); retry manually with: mnemoseed-local host install --host $_host" >&2
                fi
                ;;
        esac
    done
fi

# --- step 8: final doctor + guidance -----------------------------------------

printf '%s\n' '[8/8] doctor (final re-check)'
run_doctor
if [ "$DOCTOR_RC" -ne 0 ]; then
    printf '%s\n' 'install.sh: note: doctor still reports failures; resolve them, then re-run `mnemoseed-local doctor`' >&2
fi
printf '\n'
printf '%s\n' "installation complete: $CLI_PIN"
printf '%s\n' 'next steps:'
printf '%s\n' '  mnemoseed-local up            # start the daemon'
if [ -n "$HOST_SELECTED" ]; then
    printf '%s\n' '  (host adapters were wired above; follow the hooks/mcp/trust notes printed per host)'
else
    printf '%s\n' '  no host adapter was wired; to wire hosts, run the exact command for each host:'
    printf '%s\n' '    mnemoseed-local hook install opencode'
    printf '%s\n' '    mnemoseed-local host install --host claude_code'
    printf '%s\n' '    mnemoseed-local host install --host codex_cli'
    printf '%s\n' '    mnemoseed-local host install --host zcode'
fi
exit 0
