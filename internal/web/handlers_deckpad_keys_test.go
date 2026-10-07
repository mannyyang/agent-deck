package web

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func stubKeys(t *testing.T, fn func(profile, id string, keys []string) error) {
	t.Helper()
	prev := sendDeckpadKeys
	sendDeckpadKeys = fn
	t.Cleanup(func() { sendDeckpadKeys = prev })
}

func postKeys(srv *Server, id, body string) *httptest.ResponseRecorder {
	req := newLocalRequest(http.MethodPost, "/api/sessions/"+id+"/keys", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Origin", "http://"+req.Host)
	rr := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr, req)
	return rr
}

func TestDeckpadKeysMapsNamesToTmuxKeysInOrder(t *testing.T) {
	var got []string
	stubKeys(t, func(profile, id string, keys []string) error { got = keys; return nil })
	rr := postKeys(deckpadServer(t, true), "child-1", `{"keys":["down","down","enter","escape","tab","shift-tab","ctrl-c","up"]}`)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}
	want := "Down Down Enter Escape Tab BTab C-c Up"
	if strings.Join(got, " ") != want {
		t.Fatalf("got %q want %q", strings.Join(got, " "), want)
	}
}

func TestDeckpadKeysAcceptsLeftAndRight(t *testing.T) {
	var got []string
	stubKeys(t, func(profile, id string, keys []string) error { got = keys; return nil })
	postKeys(deckpadServer(t, true), "child-1", `{"keys":["left","right"]}`)
	if strings.Join(got, " ") != "Left Right" {
		t.Fatalf("got %v", got)
	}
}

func TestDeckpadKeysRejectsUnknownEmptyAndTooManyKeys(t *testing.T) {
	stubKeys(t, func(profile, id string, keys []string) error { t.Fatal("must not send"); return nil })
	srv := deckpadServer(t, true)
	for name, body := range map[string]string{
		"unknown":   `{"keys":["rm -rf"]}`,
		"empty":     `{"keys":[]}`,
		"missing":   `{}`,
		"too many":  `{"keys":["up","up","up","up","up","up","up","up","up"]}`,
		"bad json":  `nope`,
		"tmux flag": `{"keys":["-t"]}`,
	} {
		if rr := postKeys(srv, "child-1", body); rr.Code != http.StatusBadRequest {
			t.Errorf("%s: expected 400, got %d", name, rr.Code)
		}
	}
}

func TestDeckpadKeysRefusedWhenReadOnlyAndNotFoundForUnknownSession(t *testing.T) {
	stubKeys(t, func(profile, id string, keys []string) error {
		if id == "gone" {
			return errDeckpadSessionNotFound
		}
		return nil
	})
	if rr := postKeys(deckpadServer(t, false), "child-1", `{"keys":["up"]}`); rr.Code != http.StatusForbidden {
		t.Errorf("read-only: expected 403, got %d", rr.Code)
	}
	if rr := postKeys(deckpadServer(t, true), "gone", `{"keys":["up"]}`); rr.Code != http.StatusNotFound {
		t.Errorf("unknown session: expected 404, got %d", rr.Code)
	}
}
