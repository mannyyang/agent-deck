package session

import (
	"strings"
	"testing"
)

// TestEmbeddedBridgeHasCustomizationMarkers fails the build BEFORE pytest runs if
// a future upstream merge overwrites the embedded bridge and drops one of our
// fork-local customizations. Each marker maps to a customization re-applied onto
// upstream's go:embed'd bridge (see the port commit + conductor/tests/
// test_bridge_customizations.py for the behavioral coverage).
//
// Runs under `go test ./internal/session/ -run Embed` (name contains "Embed").
func TestEmbeddedBridgeHasCustomizationMarkers(t *testing.T) {
	markers := []struct {
		customization string
		marker        string
	}{
		{"secret resolution (env/keychain)", "_resolve_secret"},
		{"subprocess process-group isolation", "start_new_session=True"},
		{"non-UTF-8 decode robustness", `errors="replace"`},
		{"slash command /ad-compact", `@app.command("/ad-compact")`},
		{"slash command /ad-clear", `@app.command("/ad-clear")`},
		{"slash command /ad-check", `@app.command("/ad-check")`},
		{"slash command /ad-send", `@app.command("/ad-send")`},
		{"Slack liveness watchdog", "slack_liveness_watchdog"},
		{"watchdog stale timeout (30m)", "_SLACK_STALE_TIMEOUT = 1800"},
		{"per-platform conductor filtering / mirror target", "def select_mirror_conductor"},
		{"Slack terminal mirror", "async def mirror_loop"},
		{"voice STT opt-in gate", "BRIDGE_STT_ENABLED"},
		{"voice STT backend", "parakeet-mlx"},
	}

	for _, m := range markers {
		if !strings.Contains(conductorBridgePy, m.marker) {
			t.Errorf("embedded bridge is missing the %q customization (marker %q not found). "+
				"A merge likely overwrote internal/session/conductor_bridge.py — re-apply the port.",
				m.customization, m.marker)
		}
	}
}
