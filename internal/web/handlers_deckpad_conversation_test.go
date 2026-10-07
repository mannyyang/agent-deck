package web

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/asheshgoplani/agent-deck/internal/recall/query"
)

func stubConversation(t *testing.T, fn func(profile, id string) (*deckpadConversationSource, error)) {
	t.Helper()
	prev := loadDeckpadConversation
	loadDeckpadConversation = fn
	t.Cleanup(func() { loadDeckpadConversation = prev })
}

func getConversation(srv *Server, id, query string) *httptest.ResponseRecorder {
	req := newLocalRequest(http.MethodGet, "/api/sessions/"+id+"/conversation"+query, nil)
	rr := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr, req)
	return rr
}

func convRows() []query.Row {
	return []query.Row{
		{ID: "seq:1", Kind: "user", Body: "fix the login bug", TS: "t1"},
		{ID: "seq:2", Kind: "system", Title: "Result: ok", Body: "ok"},
		{ID: "seq:3", Kind: "user", Body: "<system-reminder>noise</system-reminder>"},
		{ID: "seq:4", Kind: "bash", Title: "go test ./...", Body: "go test ./...\nmore", Meta: map[string]any{"tool_name": "Bash"}},
		{ID: "seq:5", Kind: "tool", Title: "Read", Body: "/tmp/a.go"},
		{ID: "seq:6", Kind: "other"},
		{ID: "seq:6b", Kind: "tool"},
		{ID: "seq:7", Kind: "assistant", Body: "  Fixed. Tests pass.  ", TS: "t7"},
		{ID: "seq:8", Kind: "assistant", Body: "   "},
	}
}

func TestDeckpadConversationItemsKeepsPromptsRepliesAndOneLineTools(t *testing.T) {
	items, truncated := deckpadConversationItems(convRows(), 50)
	if truncated {
		t.Fatal("did not expect truncation")
	}
	var got []string
	for _, it := range items {
		got = append(got, it.Kind+"|"+it.Tool+"|"+it.Text)
	}
	want := []string{
		"user||fix the login bug",
		"tool|Bash|go test ./...",
		"tool|Read|/tmp/a.go",
		"assistant||Fixed. Tests pass.",
	}
	if strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("got:\n%s\nwant:\n%s", strings.Join(got, "\n"), strings.Join(want, "\n"))
	}
}

func TestDeckpadConversationItemsKeepsNewestWhenOverLimit(t *testing.T) {
	items, truncated := deckpadConversationItems(convRows(), 2)
	if !truncated || len(items) != 2 || items[1].Kind != "assistant" || items[0].Tool != "Read" {
		t.Fatalf("unexpected: truncated=%v items=%+v", truncated, items)
	}
}

func TestDeckpadConversationEndpointServesItemsScreenAndVersion(t *testing.T) {
	stubConversation(t, func(profile, id string) (*deckpadConversationSource, error) {
		if id != "child-1" {
			return nil, errDeckpadSessionNotFound
		}
		return &deckpadConversationSource{Tool: "claude", Rows: convRows(), Screen: "Do you want to proceed?\n 1. Yes", ScreenIsDialog: true}, nil
	})
	srv := deckpadServer(t, false)
	rr := getConversation(srv, "child-1", "")
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}
	var resp deckpadConversationResponse
	if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if len(resp.Items) != 4 || resp.Screen == "" || resp.Version == "" || resp.Unchanged {
		t.Fatalf("unexpected: %+v", resp)
	}
	rr = getConversation(srv, "child-1", "?ifVersion="+resp.Version)
	var again deckpadConversationResponse
	_ = json.Unmarshal(rr.Body.Bytes(), &again)
	if !again.Unchanged || len(again.Items) != 0 {
		t.Fatalf("expected unchanged with no items, got %+v", again)
	}
	if rr := getConversation(srv, "nope", ""); rr.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", rr.Code)
	}
}

const deckpadIdlePane = `  some earlier output
────────────────────────────────────────────────────────────────────────────────────── deckpad ─
❯ 
────────────────────────────────────────────────────────────────────────────────────────────────
  ~/git/deckpad | main | Opus 5.5 | 16% ctx (164.1k/1000k) | 3h 31m
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
`

func TestDeckpadStatusLineTakesTheLineBelowThePromptBox(t *testing.T) {
	got := deckpadStatusLine(deckpadIdlePane)
	want := "~/git/deckpad | main | Opus 5.5 | 16% ctx (164.1k/1000k) | 3h 31m"
	if got != want {
		t.Fatalf("got %q want %q", got, want)
	}
}

func TestDeckpadStatusLineIsEmptyForDialogsAndBarePanes(t *testing.T) {
	dialog := "❯ 1. Yes\n   2. No\n────────────────────────────────────────────────────────────\n  Chat about this\n\nEnter to select · ↑/↓ to navigate · Esc to cancel\n"
	if got := deckpadStatusLine(dialog); got != "" {
		t.Fatalf("dialog should give no status line, got %q", got)
	}
	if got := deckpadStatusLine("just a shell prompt $ \n"); got != "" {
		t.Fatalf("no rule should give no status line, got %q", got)
	}
	if got := deckpadStatusLine(""); got != "" {
		t.Fatalf("empty pane: %q", got)
	}
}

func TestDeckpadHasDialogRecognisesPromptsAndIgnoresIdlePanes(t *testing.T) {
	yes := []string{
		"Do you want to proceed?\n ❯ 1. Yes\n   2. No\n\nEsc to cancel",
		"❯ 1. Yes, update the SSM parameter\n  2. No\nEnter to select · ↑/↓ to navigate · Esc to cancel",
		"Allow this command to run? (y/n)",
		"Overwrite file? [Y/n]",
		"Do you want to make this edit to foo.go?\n 1. Yes\n 2. Yes, allow all edits during this session\n 3. No",
	}
	for _, p := range yes {
		if !deckpadHasDialog(p) {
			t.Errorf("expected a dialog in:\n%s", p)
		}
	}
	if deckpadHasDialog(deckpadIdlePane) {
		t.Error("idle prompt box is not a dialog")
	}
	if deckpadHasDialog("I fixed it.\nShould I run the tests?\n❯ \n") {
		t.Error("a question in prose is not a dialog")
	}
	if deckpadHasDialog("") {
		t.Error("empty pane is not a dialog")
	}
}

func TestDeckpadConversationOmitsScreenWithoutADialog(t *testing.T) {
	stubConversation(t, func(profile, id string) (*deckpadConversationSource, error) {
		return &deckpadConversationSource{Tool: "claude", Rows: convRows(), Screen: "just the idle prompt", ScreenIsDialog: false}, nil
	})
	rr := getConversation(deckpadServer(t, false), "child-1", "")
	var resp deckpadConversationResponse
	_ = json.Unmarshal(rr.Body.Bytes(), &resp)
	if resp.Screen != "" {
		t.Fatalf("screen should be empty without a dialog, got %q", resp.Screen)
	}
}
