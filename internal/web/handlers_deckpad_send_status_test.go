package web

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func getSendStatus(srv *Server, id, sendID string) *httptest.ResponseRecorder {
	req := newLocalRequest(http.MethodGet, "/api/sessions/"+id+"/send-status/"+sendID, nil)
	rr := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr, req)
	return rr
}

func TestDeckpadSendStatusReadsCLI(t *testing.T) {
	var gotArgs []string
	stubCLI(t, func(ctx context.Context, profile string, args ...string) ([]byte, int, error) {
		gotArgs = args
		return []byte(`{"success":true,"send_id":"snd-1","state":"landed","verdict":"landed","reason":"user row","settled":true}`), 0, nil
	})
	rr := getSendStatus(deckpadServer(t, false), "child-1", "snd-1")
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}
	var resp map[string]any
	if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if resp["state"] != "landed" || resp["settled"] != true {
		t.Fatalf("unexpected body: %s", rr.Body.String())
	}
	if strings.Join(gotArgs, " ") != "session send-status snd-1 --json" {
		t.Fatalf("args = %v", gotArgs)
	}
}

func TestDeckpadSendStatusWorksInReadOnly(t *testing.T) {
	stubCLI(t, func(context.Context, string, ...string) ([]byte, int, error) {
		return []byte(`{"success":true,"send_id":"snd-1","state":"queued","verdict":"queued"}`), 0, nil
	})
	srv := NewServer(Config{ListenAddr: "127.0.0.1:0", ReadOnly: true, Profile: "personal"})
	srv.menuData = &fakeMenuDataLoader{snapshot: ccTestMenu()}
	rr := getSendStatus(srv, "child-1", "snd-1")
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200 in read-only, got %d: %s", rr.Code, rr.Body.String())
	}
}

func TestDeckpadSendStatusUnknownIDIs404(t *testing.T) {
	stubCLI(t, func(context.Context, string, ...string) ([]byte, int, error) {
		return []byte(`{"success":false,"error":"unknown send id","code":"SEND_NOT_FOUND"}`), 2, nil
	})
	rr := getSendStatus(deckpadServer(t, false), "child-1", "nope")
	if rr.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d: %s", rr.Code, rr.Body.String())
	}
}

func TestDeckpadSendStatusRejectsBadSendID(t *testing.T) {
	rr := getSendStatus(deckpadServer(t, false), "child-1", "..%2Fetc")
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rr.Code)
	}
}
