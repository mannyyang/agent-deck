package web

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func getOutput(srv *Server, id, query string) *httptest.ResponseRecorder {
	req := newLocalRequest(http.MethodGet, "/api/sessions/"+id+"/output"+query, nil)
	rr := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr, req)
	return rr
}

func TestDeckpadOutputReturnsLastResponse(t *testing.T) {
	var gotArgs []string
	stubCLI(t, func(ctx context.Context, profile string, args ...string) ([]byte, int, error) {
		gotArgs = args
		return []byte(`{"success":true,"session_id":"child-1","session_title":"fix-1431","tool":"claude","role":"assistant","content":"Done. Tests pass.","timestamp":"2026-10-07T10:00:00Z","content_version":"v7"}`), 0, nil
	})
	rr := getOutput(deckpadServer(t, false), "child-1", "")
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}
	var resp map[string]any
	if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if resp["content"] != "Done. Tests pass." || resp["contentVersion"] != "v7" || resp["tool"] != "claude" {
		t.Fatalf("unexpected body: %s", rr.Body.String())
	}
	if strings.Join(gotArgs, " ") != "session output child-1 --json" {
		t.Fatalf("args = %v", gotArgs)
	}
}

func TestDeckpadOutputPassesIfVersion(t *testing.T) {
	var gotArgs []string
	stubCLI(t, func(ctx context.Context, profile string, args ...string) ([]byte, int, error) {
		gotArgs = args
		return []byte(`{"success":true,"unchanged":true}`), 0, nil
	})
	rr := getOutput(deckpadServer(t, false), "child-1", "?ifVersion=v7")
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}
	if !strings.Contains(rr.Body.String(), `"unchanged":true`) {
		t.Fatalf("expected unchanged, got %s", rr.Body.String())
	}
	if strings.Join(gotArgs, " ") != "session output child-1 --json --if-version v7" {
		t.Fatalf("args = %v", gotArgs)
	}
}

func TestDeckpadOutputNoResponseYetIsEmptyNotError(t *testing.T) {
	stubCLI(t, func(context.Context, string, ...string) ([]byte, int, error) {
		return []byte(`{"success":false,"error":"no response found for session","code":"NO_OUTPUT"}`), 1, nil
	})
	rr := getOutput(deckpadServer(t, false), "child-1", "")
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200 for empty output, got %d: %s", rr.Code, rr.Body.String())
	}
	if !strings.Contains(rr.Body.String(), `"empty":true`) {
		t.Fatalf("expected empty marker, got %s", rr.Body.String())
	}
}

func TestDeckpadOutputUnknownSessionIs404(t *testing.T) {
	stubCLI(t, func(context.Context, string, ...string) ([]byte, int, error) {
		return []byte(`{"success":false,"error":"session not found: nope","code":"SESSION_NOT_FOUND"}`), 1, nil
	})
	rr := getOutput(deckpadServer(t, false), "nope", "")
	if rr.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d: %s", rr.Code, rr.Body.String())
	}
}

func TestDeckpadOutputRequiresTokenWhenConfigured(t *testing.T) {
	srv := NewServer(Config{ListenAddr: "127.0.0.1:0", Token: "secret", Profile: "personal"})
	srv.menuData = &fakeMenuDataLoader{snapshot: ccTestMenu()}
	rr := getOutput(srv, "child-1", "")
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 without token, got %d", rr.Code)
	}
}
