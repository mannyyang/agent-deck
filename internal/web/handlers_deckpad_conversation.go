package web

import (
	"context"
	"crypto/sha1"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/asheshgoplani/agent-deck/internal/recall/query"
	"github.com/asheshgoplani/agent-deck/internal/session"
	"github.com/asheshgoplani/agent-deck/internal/tmux"
)

// GET /api/sessions/{id}/conversation: the phone's session screen. The
// native transcript, filtered to what a person reads (their prompts, the
// agent's replies, one line per tool call), plus the tail of the live
// terminal screen, which is the only place a pending approval prompt shows.

const (
	deckpadConversationDefaultLimit = 120
	deckpadConversationMaxLimit     = 400
	deckpadConversationTextRunes    = 6000
	deckpadConversationToolRunes    = 160
	deckpadConversationScreenLines  = 24
)

var errDeckpadSessionNotFound = errors.New("session not found")

type deckpadConversationItem struct {
	ID   string `json:"id"`
	Kind string `json:"kind"` // user | assistant | tool
	Text string `json:"text"`
	Tool string `json:"tool,omitempty"`
	TS   string `json:"ts,omitempty"`
}

type deckpadConversationResponse struct {
	SessionID string                    `json:"sessionId"`
	Tool      string                    `json:"tool,omitempty"`
	Items     []deckpadConversationItem `json:"items"`
	Screen    string                    `json:"screen"`
	// StatusLine is the harness's own status line, as printed under the
	// prompt box (folder, branch, model, context, spend), or "".
	StatusLine string `json:"statusLine"`
	Version    string `json:"version"`
	Unchanged  bool   `json:"unchanged"`
	Truncated  bool   `json:"truncated"`
}

type deckpadConversationSource struct {
	Tool       string
	Rows       []query.Row
	Screen     string
	StatusLine string
}

// Harness-injected user records that are not something the person typed.
var deckpadNoisePrefixes = []string{
	"<system-reminder>", "<local-command", "<command-name>", "<command-message>",
	"<task-notification>", "<user-prompt-submit-hook>", "Caveat: The messages below",
}

func deckpadTruncate(s string, max int) string {
	r := []rune(s)
	if len(r) <= max {
		return s
	}
	return strings.TrimSpace(string(r[:max])) + "…"
}

// deckpadConversationItems filters parsed rows down to prompts, replies and
// one-line tool calls, keeping the newest limit items.
func deckpadConversationItems(rows []query.Row, limit int) ([]deckpadConversationItem, bool) {
	items := make([]deckpadConversationItem, 0, len(rows))
	for _, r := range rows {
		switch r.Kind {
		case "user", "assistant":
			text := strings.TrimSpace(r.Body)
			if text == "" {
				continue
			}
			if r.Kind == "user" {
				noise := false
				for _, p := range deckpadNoisePrefixes {
					if strings.HasPrefix(text, p) {
						noise = true
						break
					}
				}
				if noise {
					continue
				}
			}
			items = append(items, deckpadConversationItem{ID: r.ID, Kind: r.Kind, Text: deckpadTruncate(text, deckpadConversationTextRunes), TS: r.TS})
		case "tool", "bash", "edit", "todo", "subagent", "skill":
			name := r.Title
			if r.Kind != "tool" {
				name = r.Kind
			}
			if n, ok := r.Meta["tool_name"].(string); ok && n != "" {
				name = n
			}
			line := strings.TrimSpace(r.Body)
			if i := strings.IndexByte(line, '\n'); i >= 0 {
				line = line[:i]
			}
			if name == "" && line == "" {
				continue
			}
			items = append(items, deckpadConversationItem{ID: r.ID, Kind: "tool", Tool: name, Text: deckpadTruncate(line, deckpadConversationToolRunes), TS: r.TS})
		}
	}
	if limit > 0 && len(items) > limit {
		return items[len(items)-limit:], true
	}
	return items, false
}

// Lines under the prompt box that are key hints or dialog chrome, not a status line.
var deckpadHintMarkers = []string{
	"shift+tab", "bypass permissions", "? for shortcuts", "esc to interrupt", "ctrl+",
	"enter to select", "esc to cancel", "accept edits", "plan mode", "auto-accept",
}

func deckpadIsRule(line string) bool {
	t := strings.TrimSpace(line)
	if len([]rune(t)) < 10 {
		return false
	}
	runes := 0
	for _, r := range t {
		if r == '─' || r == '━' || r == '-' || r == '═' {
			runes++
		}
	}
	return runes*10 >= len([]rune(t))*6
}

// deckpadStatusLine finds the harness's status line: the first line under
// the last horizontal rule that is not the prompt or a key hint. Empty when
// the pane shows a dialog or has no rule.
func deckpadStatusLine(raw string) string {
	lines := strings.Split(raw, "\n")
	last := -1
	for i, l := range lines {
		if deckpadIsRule(l) {
			last = i
		}
	}
	if last < 0 {
		return ""
	}
	var keep []string
	for _, l := range lines[last+1:] {
		t := strings.TrimSpace(tmux.StripANSI(l))
		if t == "" || strings.HasPrefix(t, "❯") || strings.HasPrefix(t, ">") || strings.HasPrefix(t, "⏵") || strings.HasPrefix(t, "⏸") {
			continue
		}
		low := strings.ToLower(t)
		hint := false
		for _, m := range deckpadHintMarkers {
			if strings.Contains(low, m) {
				hint = true
				break
			}
		}
		if hint {
			return "" // a dialog or mode hint sits here, so this is not a status line
		}
		keep = append(keep, t)
	}
	if len(keep) == 0 || len(keep) > 2 {
		return ""
	}
	return deckpadTruncate(strings.Join(keep, "\n"), 240)
}

// deckpadScreenTail strips colour codes and keeps the last non-blank lines.
func deckpadScreenTail(raw string) string {
	lines := strings.Split(tmux.StripANSI(raw), "\n")
	out := make([]string, 0, len(lines))
	for _, l := range lines {
		out = append(out, strings.TrimRight(l, " \t\r"))
	}
	for len(out) > 0 && out[len(out)-1] == "" {
		out = out[:len(out)-1]
	}
	if len(out) > deckpadConversationScreenLines {
		out = out[len(out)-deckpadConversationScreenLines:]
	}
	return strings.Join(out, "\n")
}

type deckpadRowsCacheEntry struct {
	path string
	size int64
	mod  time.Time
	rows []query.Row
}

var deckpadRowsCache = struct {
	mu sync.Mutex
	m  map[string]deckpadRowsCacheEntry
}{m: map[string]deckpadRowsCacheEntry{}}

// deckpadTranscriptRows parses a transcript, reusing the last parse while
// the file is unchanged (the phone polls every couple of seconds).
func deckpadTranscriptRows(ctx context.Context, id, harness, path, nativeID string) ([]query.Row, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	deckpadRowsCache.mu.Lock()
	defer deckpadRowsCache.mu.Unlock()
	if e, ok := deckpadRowsCache.m[id]; ok && e.path == path && e.size == info.Size() && e.mod.Equal(info.ModTime()) {
		return e.rows, nil
	}
	rows, err := query.TimelineTurnsForSource(ctx, harness, path, nativeID)
	if err != nil {
		return nil, err
	}
	deckpadRowsCache.m[id] = deckpadRowsCacheEntry{path: path, size: info.Size(), mod: info.ModTime(), rows: rows}
	return rows, nil
}

// loadDeckpadConversation is the seam tests stub.
var loadDeckpadConversation = func(profile, id string) (*deckpadConversationSource, error) {
	svc := NewSessionDataService(profile)
	storage, _, err := svc.resolveAndOpenStorage()
	if err != nil {
		return nil, err
	}
	defer func() { _ = storage.Close() }()
	instances, _, err := storage.LoadWithGroups()
	if err != nil {
		return nil, err
	}
	var inst *session.Instance
	for _, i := range instances {
		if i.ID == id {
			inst = i
			break
		}
	}
	if inst == nil {
		return nil, errDeckpadSessionNotFound
	}
	src := &deckpadConversationSource{Tool: inst.Tool}
	harness := inst.Tool
	nativeID := inst.ClaudeSessionID
	switch {
	case session.IsClaudeCompatible(inst.Tool):
		harness = "claude"
	case session.IsCodexCompatible(inst.Tool):
		harness, nativeID = "codex", inst.CodexSessionID
	}
	if path := session.LiveTranscriptPath(inst, instances); path != "" {
		ctx, cancel := context.WithTimeout(context.Background(), deckpadCLITimeout)
		defer cancel()
		if rows, err := deckpadTranscriptRows(ctx, id, harness, path, nativeID); err == nil {
			src.Rows = rows
		}
	}
	if ts := inst.GetTmuxSession(); ts != nil {
		if raw, err := ts.CapturePane(); err == nil {
			src.Screen = deckpadScreenTail(raw)
			src.StatusLine = deckpadStatusLine(tmux.StripANSI(raw))
		}
	}
	return src, nil
}

func (s *Server) handleDeckpadConversation(w http.ResponseWriter, r *http.Request, sessionID string) {
	if r.Method != http.MethodGet {
		writeAPIError(w, http.StatusMethodNotAllowed, ErrCodeMethodNotAllowed, "method not allowed")
		return
	}
	if !validDeckpadSessionID(sessionID) {
		writeAPIError(w, http.StatusBadRequest, ErrCodeBadRequest, "invalid session id")
		return
	}
	limit := deckpadConversationDefaultLimit
	if n, err := strconv.Atoi(r.URL.Query().Get("limit")); err == nil && n > 0 {
		limit = min(n, deckpadConversationMaxLimit)
	}
	src, err := loadDeckpadConversation(s.cfg.Profile, sessionID)
	if errors.Is(err, errDeckpadSessionNotFound) {
		writeAPIError(w, http.StatusNotFound, ErrCodeNotFound, "session not found")
		return
	}
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, ErrCodeInternalError, "failed to load conversation")
		return
	}
	items, truncated := deckpadConversationItems(src.Rows, limit)
	h := sha1.New()
	fmt.Fprintf(h, "%d|%d|", limit, len(items))
	if n := len(items); n > 0 {
		fmt.Fprintf(h, "%s|%s|%d|", items[0].ID, items[n-1].ID, len(items[n-1].Text))
	}
	h.Write([]byte(src.Screen))
	h.Write([]byte(src.StatusLine))
	version := hex.EncodeToString(h.Sum(nil))[:16]
	resp := deckpadConversationResponse{SessionID: sessionID, Tool: src.Tool, Items: items, Screen: src.Screen, StatusLine: src.StatusLine, Version: version, Truncated: truncated}
	if v := r.URL.Query().Get("ifVersion"); v != "" && v == version {
		resp.Items, resp.Screen, resp.Unchanged = []deckpadConversationItem{}, "", true
	}
	writeJSON(w, http.StatusOK, resp)
}
