// Deckpad endpoints (send, send-status, output) shell out to the agent-deck
// CLI the same way the command-center ask handler does. This file holds the
// one seam tests stub so no handler test spawns a process.
package web

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"strings"
	"time"
)

const deckpadCLITimeout = 30 * time.Second

// runAgentDeckCLI runs `agent-deck -p <profile> <args...>` and returns the
// combined output, the exit code (-1 when the process did not run), and an
// error only for "could not run" or timeout. A non-zero exit with output is
// not an error here; decodeCLIEnvelope reads the JSON body instead.
var runAgentDeckCLI = func(ctx context.Context, profile string, args ...string) ([]byte, int, error) {
	exe, err := os.Executable()
	if err != nil || exe == "" {
		exe = "agent-deck"
	}
	full := append([]string{"-p", profile}, args...)
	cmd := exec.CommandContext(ctx, exe, full...)
	cmd.Env = os.Environ()
	out, runErr := cmd.CombinedOutput()
	if ctx.Err() != nil {
		return out, -1, ctx.Err()
	}
	var exitErr *exec.ExitError
	if errors.As(runErr, &exitErr) {
		return out, exitErr.ExitCode(), nil
	}
	if runErr != nil {
		return out, -1, runErr
	}
	return out, 0, nil
}

type deckpadCLIError struct {
	Status  int
	Code    string
	Message string
}

// decodeCLIEnvelope turns a CLI --json reply into either the decoded map or
// an HTTP-shaped error: 504 on timeout, 404 when the CLI's code mentions
// NOT_FOUND, 502 for every other failure, with the CLI's own message.
func decodeCLIEnvelope(out []byte, exitCode int, runErr error) (map[string]any, *deckpadCLIError) {
	if errors.Is(runErr, context.DeadlineExceeded) {
		return nil, &deckpadCLIError{504, "CLI_TIMEOUT", "agent-deck did not answer in time"}
	}
	var m map[string]any
	if json.Unmarshal([]byte(strings.TrimSpace(string(out))), &m) != nil {
		msg := strings.TrimSpace(string(out))
		if msg == "" && runErr != nil {
			msg = runErr.Error()
		}
		if msg == "" {
			msg = "agent-deck returned no output"
		}
		return nil, &deckpadCLIError{502, "CLI_FAILED", msg}
	}
	if ok, _ := m["success"].(bool); ok {
		return m, nil
	}
	// Some CLI commands (session send-status) print a raw record with no
	// "success" key; exit 0 plus parseable JSON is a success.
	if _, has := m["success"]; !has && exitCode == 0 {
		return m, nil
	}
	code, _ := m["code"].(string)
	msg, _ := m["error"].(string)
	if msg == "" {
		msg = "agent-deck command failed"
	}
	if strings.Contains(code, "NOT_FOUND") {
		return nil, &deckpadCLIError{404, ErrCodeNotFound, msg}
	}
	return nil, &deckpadCLIError{502, "CLI_FAILED", msg}
}

func stringField(m map[string]any, key string) string {
	v, _ := m[key].(string)
	return v
}

// validDeckpadSessionID rejects ids the dispatcher could not have produced
// cleanly: empty, whitespace, or path-walking.
func validDeckpadSessionID(id string) bool {
	if id == "" || strings.ContainsAny(id, " \t\r\n") || strings.Contains(id, "..") {
		return false
	}
	return true
}
