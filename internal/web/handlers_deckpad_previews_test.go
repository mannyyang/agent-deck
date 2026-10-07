package web

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func stubPreviews(t *testing.T, fn func(profile string) ([]deckpadPreviewRow, error)) {
	t.Helper()
	prev := loadDeckpadPreviews
	loadDeckpadPreviews = fn
	deckpadPreviewCache.reset()
	t.Cleanup(func() { loadDeckpadPreviews = prev; deckpadPreviewCache.reset() })
}

func getPreviews(srv *Server) *httptest.ResponseRecorder {
	req := newLocalRequest(http.MethodGet, "/api/deckpad/previews", nil)
	rr := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr, req)
	return rr
}

func TestDeckpadPreviewsShapesAndTruncates(t *testing.T) {
	long := strings.Repeat("word ", 100)
	stubPreviews(t, func(profile string) ([]deckpadPreviewRow, error) {
		return []deckpadPreviewRow{
			{ID: "a", Content: "  Done.\n\nTests   pass. ", Timestamp: "2026-10-07T10:00:00Z"},
			{ID: "b", Content: long},
		}, nil
	})
	rr := getPreviews(deckpadServer(t, false))
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}
	var resp struct {
		Previews map[string]struct {
			Preview   string `json:"preview"`
			Timestamp string `json:"timestamp"`
		} `json:"previews"`
	}
	if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if resp.Previews["a"].Preview != "Done. Tests pass." || resp.Previews["a"].Timestamp != "2026-10-07T10:00:00Z" {
		t.Fatalf("unexpected a: %+v", resp.Previews["a"])
	}
	if n := len([]rune(resp.Previews["b"].Preview)); n > deckpadPreviewMaxRunes+1 {
		t.Fatalf("preview not truncated: %d runes", n)
	}
	if !strings.HasSuffix(resp.Previews["b"].Preview, "…") {
		t.Fatalf("truncated preview should end with ellipsis: %q", resp.Previews["b"].Preview)
	}
}

func TestDeckpadPreviewsCachesForTTL(t *testing.T) {
	calls := 0
	stubPreviews(t, func(profile string) ([]deckpadPreviewRow, error) {
		calls++
		return []deckpadPreviewRow{{ID: "a", Content: "x"}}, nil
	})
	srv := deckpadServer(t, false)
	getPreviews(srv)
	getPreviews(srv)
	if calls != 1 {
		t.Fatalf("expected one loader call within TTL, got %d", calls)
	}
	deckpadPreviewCache.expireForTest(time.Now().Add(-time.Hour))
	getPreviews(srv)
	if calls != 2 {
		t.Fatalf("expected reload after TTL, got %d", calls)
	}
}

func TestDeckpadPreviewsRequiresToken(t *testing.T) {
	srv := NewServer(Config{ListenAddr: "127.0.0.1:0", Token: "secret", Profile: "personal"})
	srv.menuData = &fakeMenuDataLoader{snapshot: ccTestMenu()}
	rr := getPreviews(srv)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", rr.Code)
	}
}
