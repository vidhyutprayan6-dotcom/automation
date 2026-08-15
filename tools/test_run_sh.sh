#!/usr/bin/env bash
#
# Exercises run.sh without a Mac.
#
# Each case puts fake command-line tools ahead of the real ones on PATH, so the
# script can be pushed down every branch — wrong OS, missing files, no python3,
# a python3 stub that cannot run, a failed install, and a clean start — and the
# message it prints can be checked. The final case confirms it reaches the
# point of launching main.py and forwards its arguments.

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

pass=0
fail=0

# A sandbox copy of the project, so tests never touch the real .venv.
setup() {
  rm -rf "$WORK/proj" "$WORK/bin"
  mkdir -p "$WORK/proj" "$WORK/bin"
  cp "$ROOT/run.sh" "$WORK/proj/"
  for f in main.py stripe_vision.py requirements.txt data.txt card.txt email.txt; do
    printf 'placeholder\n' > "$WORK/proj/$f"
  done
}

stub() {  # stub <name> <body>
  printf '#!/usr/bin/env bash\n%s\n' "$2" > "$WORK/bin/$1"
  chmod +x "$WORK/bin/$1"
}

# A venv whose python records how it was called.
fake_venv() {
  mkdir -p "$WORK/proj/.venv/bin"
  cat > "$WORK/proj/.venv/bin/python" <<'PY'
#!/usr/bin/env bash
echo "python $*" >> "$RUNLOG"
case "${1:-}" in
  -c) exit "${IMPORT_RC:-0}" ;;
  -m) exit "${PIP_RC:-0}" ;;
esac
exit 0
PY
  chmod +x "$WORK/proj/.venv/bin/python"
}

check() {  # check <name> <expected-substring> <expected-rc>
  local name="$1" needle="$2" want_rc="$3"
  local out rc
  out="$(cd "$WORK/proj" && PATH="$WORK/bin:$PATH" bash run.sh 2>&1)"
  rc=$?
  if [[ "$out" == *"$needle"* ]] && [[ "$rc" -eq "$want_rc" ]]; then
    echo "ok   $name"
    pass=$((pass + 1))
  else
    echo "FAIL $name  (rc=$rc, wanted $want_rc)"
    echo "     wanted to see: $needle"
    echo "$out" | sed 's/^/       | /'
    fail=$((fail + 1))
  fi
}

echo "== syntax =="
if bash -n "$ROOT/run.sh"; then
  echo "ok   run.sh parses"
  pass=$((pass + 1))
else
  echo "FAIL run.sh has a syntax error"
  fail=$((fail + 1))
fi

echo "== branches =="

setup
stub uname 'echo Linux'
check "refuses a non-Mac with the OS named" "This machine reports: Linux" 1

setup
stub uname 'echo Darwin'
rm "$WORK/proj/stripe_vision.py" "$WORK/proj/card.txt"
check "names every missing file" "stripe_vision.py card.txt" 1

setup
stub uname 'echo Darwin'
# A PATH holding the shell utilities but no python3 at all.
BARE_PATH="$WORK/bin:/usr/bin:/bin"
if PATH="$BARE_PATH" command -v python3 >/dev/null 2>&1; then
  echo "skip reports a missing python3  (this machine has python3 in /usr/bin)"
else
  out="$(cd "$WORK/proj" && PATH="$BARE_PATH" bash run.sh 2>&1)"
  rc=$?
  if [[ "$out" == *"python3 is not installed"* ]] && [[ "$rc" -eq 1 ]]; then
    echo "ok   reports a missing python3"
    pass=$((pass + 1))
  else
    echo "FAIL missing python3 not reported (rc=$rc)"
    echo "$out" | sed 's/^/       | /'
    fail=$((fail + 1))
  fi
fi

setup
stub uname 'echo Darwin'
stub python3 'exit 1'   # the Command Line Tools stub: present but not runnable
check "spots a python3 that cannot run" "xcode-select --install" 1

setup
stub uname 'echo Darwin'
stub python3 'exit 0'
fake_venv
export RUNLOG="$WORK/run.log"; : > "$RUNLOG"
export IMPORT_RC=1 PIP_RC=1
check "reports a failed dependency install" "no internet connection" 1
unset IMPORT_RC PIP_RC

setup
stub uname 'echo Darwin'
stub python3 'exit 0'
fake_venv
export RUNLOG="$WORK/run.log"; : > "$RUNLOG"
out="$(cd "$WORK/proj" && PATH="$WORK/bin:$PATH" bash run.sh 2>&1)"
rc=$?
if [[ "$rc" -eq 0 ]] && grep -q 'python main.py' "$RUNLOG" \
   && [[ "$out" == *"Dependencies already installed."* ]]; then
  echo "ok   clean start skips pip and launches main.py"
  pass=$((pass + 1))
else
  echo "FAIL clean start (rc=$rc)"
  echo "$out" | sed 's/^/       | /'
  cat "$RUNLOG" | sed 's/^/       log| /'
  fail=$((fail + 1))
fi

setup
stub uname 'echo Darwin'
stub python3 'exit 0'
fake_venv
export RUNLOG="$WORK/run.log"; : > "$RUNLOG"
(cd "$WORK/proj" && PATH="$WORK/bin:$PATH" bash run.sh --limit 2 >/dev/null 2>&1)
if grep -q 'python main.py --limit 2' "$RUNLOG"; then
  echo "ok   forwards command-line arguments"
  pass=$((pass + 1))
else
  echo "FAIL arguments not forwarded"
  cat "$RUNLOG" | sed 's/^/       log| /'
  fail=$((fail + 1))
fi

setup
stub uname 'echo Darwin'
stub python3 'exit 0'
fake_venv
export RUNLOG="$WORK/run.log"; : > "$RUNLOG"
export IMPORT_RC=1 PIP_RC=0
out="$(cd "$WORK/proj" && PATH="$WORK/bin:$PATH" bash run.sh 2>&1)"
if [[ "$out" == *"Installing dependencies"* ]]; then
  echo "ok   installs when an import is missing"
  pass=$((pass + 1))
else
  echo "FAIL install not attempted"
  echo "$out" | sed 's/^/       | /'
  fail=$((fail + 1))
fi
unset IMPORT_RC PIP_RC

echo "------------------------------------------------------------"
echo "$pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
