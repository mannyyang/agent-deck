package web

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func stubCLI(t *testing.T, fn func(ctx context.Context, profile string, args ...string) ([]byte, int, error)) {
	t.Helper()
	prev := runAgentDeckCLI
	runAgentDeckCLI = fn
	t.Cleanup(func() { runAgentDeckCLI = prev })
}

func deckpadServer(t *testing.T, mutations bool) *Server {
	t.Helper()
	srv := NewServer(Config{ListenAddr: "127.0.0.1:0", WebMutations: mutations, Profile: "personal"})
	srv.menuData = &fakeMenuDataLoader{snapshot: ccTestMenu()}
	return srv
}

func postSend(srv *Server, id, body string) *httptest.ResponseRecorder {
	req := newLocalRequest(http.MethodPost, "/api/sessions/"+id+"/send", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Origin", "http://"+req.Host)
	rr := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr, req)
	return rr
}

func TestDeckpadSendQueuesViaCLI(t *testing.T) {
	var gotArgs []string
	stubCLI(t, func(ctx context.Context, profile string, args ...string) ([]byte, int, error) {
		gotArgs = append([]string{profile}, args...)
		return []byte(`{"success":true,"send_id":"snd-1","state":"queued","verdict":"queued"}`), 0, nil
	})
	rr := postSend(deckpadServer(t, true), "child-1", `{"text":"yes continue"}`)
	if rr.Code != http.StatusAccepted {
		t.Fatalf("expected 202, got %d: %s", rr.Code, rr.Body.String())
	}
	var resp map[string]string
	if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if resp["sendId"] != "snd-1" || resp["state"] != "queued" {
		t.Fatalf("unexpected body: %s", rr.Body.String())
	}
	want := []string{"personal", "session", "send", "child-1", "yes continue", "--queue", "--json"}
	if strings.Join(gotArgs, " ") != strings.Join(want, " ") {
		t.Fatalf("args = %v, want %v", gotArgs, want)
	}
}

func TestDeckpadSendRejectsEmptyText(t *testing.T) {
	stubCLI(t, func(context.Context, string, ...string) ([]byte, int, error) {
		t.Fatal("CLI must not run for empty text")
		return nil, 0, nil
	})
	rr := postSend(deckpadServer(t, true), "child-1", `{"text":"   "}`)
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rr.Code)
	}
}

func TestDeckpadSendRejectsBadJSON(t *testing.T) {
	rr := postSend(deckpadServer(t, true), "child-1", `{"text":`)
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rr.Code)
	}
}

func TestDeckpadSendRejectsBadSessionID(t *testing.T) {
	rr := postSend(deckpadServer(t, true), "bad%20id", `{"text":"hi"}`)
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 for id with whitespace, got %d", rr.Code)
	}
}

func TestDeckpadSendForbiddenWhenMutationsDisabled(t *testing.T) {
	rr := postSend(deckpadServer(t, false), "child-1", `{"text":"hi"}`)
	if rr.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", rr.Code)
	}
}

func TestDeckpadSendMapsCLINotFound(t *testing.T) {
	stubCLI(t, func(context.Context, string, ...string) ([]byte, int, error) {
		return []byte(`{"success":false,"error":"session not found: nope","code":"SESSION_NOT_FOUND"}`), 1, nil
	})
	rr := postSend(deckpadServer(t, true), "nope", `{"text":"hi"}`)
	if rr.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d: %s", rr.Code, rr.Body.String())
	}
	if !strings.Contains(rr.Body.String(), "session not found") {
		t.Fatalf("expected CLI message, got %s", rr.Body.String())
	}
}

func TestDeckpadSendMapsCLIFailure(t *testing.T) {
	stubCLI(t, func(context.Context, string, ...string) ([]byte, int, error) {
		return []byte(`{"success":false,"error":"pane gone","code":"DELIVERY_FAILED"}`), 1, nil
	})
	rr := postSend(deckpadServer(t, true), "child-1", `{"text":"hi"}`)
	if rr.Code != http.StatusBadGateway {
		t.Fatalf("expected 502, got %d: %s", rr.Code, rr.Body.String())
	}
}

func TestDeckpadSendMapsTimeout(t *testing.T) {
	stubCLI(t, func(context.Context, string, ...string) ([]byte, int, error) {
		return nil, -1, context.DeadlineExceeded
	})
	rr := postSend(deckpadServer(t, true), "child-1", `{"text":"hi"}`)
	if rr.Code != http.StatusGatewayTimeout {
		t.Fatalf("expected 504, got %d: %s", rr.Code, rr.Body.String())
	}
}

func TestDeckpadSendMapsNonJSONOutput(t *testing.T) {
	stubCLI(t, func(context.Context, string, ...string) ([]byte, int, error) {
		return []byte("panic: boom"), 2, errors.New("exit status 2")
	})
	rr := postSend(deckpadServer(t, true), "child-1", `{"text":"hi"}`)
	if rr.Code != http.StatusBadGateway {
		t.Fatalf("expected 502, got %d: %s", rr.Code, rr.Body.String())
	}
}
