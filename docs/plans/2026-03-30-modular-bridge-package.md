# Modular Bridge Package Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the monolithic `conductorBridgePy` Go string constant with the refactored bridge package (10 Python files), so `agent-deck conductor setup` installs the modular bridge instead of overwriting it.

**Architecture:** The current `InstallBridgeScript()` writes a single ~1960-line Python string to `bridge.py`. We replace this with: (1) a map of filename->content for each bridge module, (2) an updated installer that writes `bridge.py` (thin wrapper) + `bridge/*.py` (10 modules), and (3) updated teardown to clean up the package directory.

**Tech Stack:** Go (templates, file I/O), Python (bridge source embedded as Go string constants)

---

### Task 1: Replace `conductorBridgePy` with modular bridge constants

**Files:**
- Modify: `/Users/myang/git/agent-deck/internal/session/conductor_templates.go:364-2326`

**Step 1: Replace the monolithic constant with the thin wrapper + package map**

Delete the entire `conductorBridgePy` constant (lines 364-2326) and replace with:

```go
// conductorBridgePy is the thin wrapper that delegates to the bridge package.
// Kept as the entry point for launchd/systemd configs that invoke bridge.py directly.
const conductorBridgePy = `#!/usr/bin/env python3
"""
Conductor Bridge: Telegram & Slack & Discord <-> Agent-Deck conductor sessions.

Thin wrapper that delegates to the bridge package.
Kept for backward compatibility with launchd/systemd configs that invoke bridge.py directly.
"""

import asyncio
from bridge.main import main

if __name__ == "__main__":
    asyncio.run(main())
`

// conductorBridgePackage maps filenames to Python source for the bridge/ package.
// InstallBridgeScript() writes these to ~/.agent-deck/conductor/bridge/*.py.
var conductorBridgePackage = map[string]string{
    "__init__.py": conductorBridgePkgInit,
    "constants.py": conductorBridgePkgConstants,
    "config.py": conductorBridgePkgConfig,
    "cli.py": conductorBridgePkgCli,
    "formatting.py": conductorBridgePkgFormatting,
    "telegram_bot.py": conductorBridgePkgTelegramBot,
    "slack_bot.py": conductorBridgePkgSlackBot,
    "discord_bot.py": conductorBridgePkgDiscordBot,
    "heartbeat.py": conductorBridgePkgHeartbeat,
    "mirror.py": conductorBridgePkgMirror,
    "main.py": conductorBridgePkgMain,
}
```

Then add each module as its own constant. Each constant is a raw Go string (backtick-delimited) containing the Python source verbatim from `~/.agent-deck/conductor/bridge/*.py`.

**Important:** Any backtick (`` ` ``) inside the Python source must be handled. The Python files use backticks in f-strings and string literals. Use the Go raw string concatenation trick: split the constant at each backtick and rejoin with `` + "`" + `` . Alternatively, since the Python files don't contain isolated backticks outside of triple-backtick markdown strings, a simpler approach is to use a `//go:embed` directive — but that requires the files to exist at build time. Since we want the binary to be self-contained, stick with string constants but be careful with escaping.

**Approach for backtick escaping:** Go raw strings (backtick-delimited) cannot contain backticks. For each Python module that contains backticks (e.g., `constants.py` has triple-backtick code block patterns, `mirror.py` has backtick formatting), use the pattern:

```go
const conductorBridgePkgConstants = `...code before backtick...` + "`" + `...code after backtick...`
```

Modules that do NOT contain any backtick characters can use simple backtick-delimited raw strings.

**Scan of backtick usage in each module:**
- `__init__.py` — no backticks ✓
- `constants.py` — has backticks in `markdown_to_slack()` regex patterns (lines 101, 108, etc.) ⚠️
- `config.py` — no backticks ✓
- `cli.py` — no backticks ✓
- `formatting.py` — has backticks in `md_to_tg_html()` regex patterns (lines 70-71, 81) ⚠️
- `telegram_bot.py` — no backticks ✓
- `slack_bot.py` — no backticks ✓
- `discord_bot.py` — has backticks in help text (lines 273-279) ⚠️
- `heartbeat.py` — no backticks ✓
- `mirror.py` — has backticks in format_jsonl_event() for code blocks (lines 107, 137) and backtick tool formatting ⚠️
- `main.py` — no backticks ✓

**Step 2: Verify the file compiles**

Run: `cd /Users/myang/git/agent-deck && go build ./internal/session/`
Expected: clean compile

**Step 3: Commit**

```bash
cd /Users/myang/git/agent-deck
git add internal/session/conductor_templates.go
git commit -m "Replace monolithic bridge.py with modular bridge package constants"
```

---

### Task 2: Update `InstallBridgeScript()` to write the package

**Files:**
- Modify: `/Users/myang/git/agent-deck/internal/session/conductor.go:1190-1208`

**Step 1: Update the function to write both bridge.py and bridge/*.py**

Replace the existing `InstallBridgeScript()` with:

```go
// InstallBridgeScript installs bridge.py and the bridge/ package to the conductor base directory.
func InstallBridgeScript() error {
	dir, err := ConductorDir()
	if err != nil {
		return err
	}

	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("failed to create conductor dir: %w", err)
	}

	// Write thin wrapper bridge.py
	bridgePath := filepath.Join(dir, "bridge.py")
	if err := os.WriteFile(bridgePath, []byte(conductorBridgePy), 0o755); err != nil {
		return fmt.Errorf("failed to write bridge.py: %w", err)
	}

	// Write bridge/ package modules
	pkgDir := filepath.Join(dir, "bridge")
	if err := os.MkdirAll(pkgDir, 0o755); err != nil {
		return fmt.Errorf("failed to create bridge package dir: %w", err)
	}

	for filename, content := range conductorBridgePackage {
		modPath := filepath.Join(pkgDir, filename)
		if err := os.WriteFile(modPath, []byte(content), 0o644); err != nil {
			return fmt.Errorf("failed to write bridge/%s: %w", filename, err)
		}
	}

	return nil
}
```

**Step 2: Verify compile**

Run: `cd /Users/myang/git/agent-deck && go build ./internal/session/`
Expected: clean compile

**Step 3: Commit**

```bash
cd /Users/myang/git/agent-deck
git add internal/session/conductor.go
git commit -m "Update InstallBridgeScript to write modular bridge package"
```

---

### Task 3: Update teardown to clean up bridge/ directory

**Files:**
- Modify: `/Users/myang/git/agent-deck/cmd/agent-deck/conductor_cmd.go:796-806`

**Step 1: Add bridge/ directory removal to teardown**

In the teardown section (around line 799), after `_ = os.Remove(filepath.Join(condDir, "bridge.py"))`, add:

```go
// Remove bridge package directory
bridgePkgDir := filepath.Join(condDir, "bridge")
_ = os.RemoveAll(bridgePkgDir)
```

**Step 2: Verify compile**

Run: `cd /Users/myang/git/agent-deck && go build ./cmd/agent-deck/`
Expected: clean compile

**Step 3: Commit**

```bash
cd /Users/myang/git/agent-deck
git add cmd/agent-deck/conductor_cmd.go
git commit -m "Clean up bridge/ package directory on teardown --all --remove"
```

---

### Task 4: Update tests to use the package map

**Files:**
- Modify: `/Users/myang/git/agent-deck/internal/session/conductor_test.go`

**Step 1: Update test helpers**

The existing tests all do `template := conductorBridgePy` and then check `strings.Contains(template, ...)`. Since the code is now split across modules, these tests need to search the correct module. Add a helper:

```go
// allBridgeSource concatenates all bridge package sources for content checks.
func allBridgeSource() string {
	var sb strings.Builder
	sb.WriteString(conductorBridgePy)
	for _, content := range conductorBridgePackage {
		sb.WriteString("\n")
		sb.WriteString(content)
	}
	return sb.String()
}
```

Then replace every `template := conductorBridgePy` with `template := allBridgeSource()`.

**Step 2: Run the tests**

Run: `cd /Users/myang/git/agent-deck && go test ./internal/session/ -run TestBridgeTemplate -v -count=1`
Expected: all PASS

**Step 3: Commit**

```bash
cd /Users/myang/git/agent-deck
git add internal/session/conductor_test.go
git commit -m "Update bridge template tests to search across all package modules"
```

---

### Task 5: Update setup.sh (legacy shell script)

**Files:**
- Modify: `/Users/myang/git/agent-deck/conductor/setup.sh:42-44`
- Modify: `/Users/myang/git/agent-deck/conductor/bridge.py` (replace monolithic with thin wrapper)
- Create: `/Users/myang/git/agent-deck/conductor/bridge/` (directory with all modules)

**Step 1: Replace the repo's conductor/bridge.py with the thin wrapper**

Overwrite `/Users/myang/git/agent-deck/conductor/bridge.py` with the 13-line wrapper.

**Step 2: Copy the bridge package directory into the repo**

Copy all files from `~/.agent-deck/conductor/bridge/*.py` (excluding `__pycache__`) to `/Users/myang/git/agent-deck/conductor/bridge/`.

**Step 3: Update setup.sh to copy the bridge/ directory**

After line 44 (`ok "bridge.py installed"`), add:

```bash
# Copy bridge package
if [[ -d "${SCRIPT_DIR}/bridge" ]]; then
    mkdir -p "${CONDUCTOR_DIR}/bridge"
    cp "${SCRIPT_DIR}"/bridge/*.py "${CONDUCTOR_DIR}/bridge/"
    ok "bridge/ package installed"
fi
```

**Step 4: Update teardown.sh to clean up bridge/ directory**

Check if teardown.sh needs updating for bridge/ cleanup.

**Step 5: Commit**

```bash
cd /Users/myang/git/agent-deck
git add conductor/bridge.py conductor/bridge/ conductor/setup.sh
git commit -m "Add modular bridge package to conductor/ directory"
```

---

### Task 6: Build and verify end-to-end

**Step 1: Build the binary**

Run: `cd /Users/myang/git/agent-deck && make build` (or `go build -o build/agent-deck ./cmd/agent-deck/`)
Expected: clean build

**Step 2: Run all tests**

Run: `cd /Users/myang/git/agent-deck && go test ./... -count=1`
Expected: all pass

**Step 3: Test setup manually**

Backup current bridge, run the new binary's setup, and verify the installed files match:

```bash
# Backup
cp -r ~/.agent-deck/conductor/bridge ~/.agent-deck/conductor/bridge.bak
cp ~/.agent-deck/conductor/bridge.py ~/.agent-deck/conductor/bridge.py.bak

# Run new setup (use the freshly built binary)
./build/agent-deck conductor setup general

# Verify
diff ~/.agent-deck/conductor/bridge.py ~/.agent-deck/conductor/bridge.py.bak
diff -r ~/.agent-deck/conductor/bridge ~/.agent-deck/conductor/bridge.bak
```

Expected: files should match (or be updated to the latest embedded version)

**Step 4: Restore backup if needed**

```bash
cp ~/.agent-deck/conductor/bridge.py.bak ~/.agent-deck/conductor/bridge.py
cp -r ~/.agent-deck/conductor/bridge.bak/* ~/.agent-deck/conductor/bridge/
rm -rf ~/.agent-deck/conductor/bridge.bak ~/.agent-deck/conductor/bridge.py.bak
```

**Step 5: Install the new binary**

Run: `cd /Users/myang/git/agent-deck && make install` (or copy `build/agent-deck` to `~/.local/bin/agent-deck`)

**Step 6: Commit if any fixes were needed**

```bash
cd /Users/myang/git/agent-deck
git add -A
git commit -m "Fix any issues found during end-to-end verification"
```
