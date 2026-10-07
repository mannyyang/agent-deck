package web

import (
	"encoding/json"
	"errors"
	"net/http"
)

// POST /api/sessions/{id}/keys: press special keys in a session's terminal,
// the ones `send` cannot type as text (menu navigation, interrupt, Esc).
// Only a fixed set of key names is accepted, mapped to tmux key names.

const deckpadMaxKeysPerRequest = 8

var deckpadKeyNames = map[string]string{
	"up": "Up", "down": "Down", "left": "Left", "right": "Right",
	"enter": "Enter", "escape": "Escape", "tab": "Tab", "shift-tab": "BTab",
	"ctrl-c": "C-c",
}

type deckpadKeysRequest struct {
	Keys []string `json:"keys"`
}

// sendDeckpadKeys is the seam tests stub. The default finds the session's
// tmux pane and sends each named key in order.
var sendDeckpadKeys = func(profile, id string, keys []string) error {
	svc := NewSessionDataService(profile)
	storage, _, err := svc.resolveAndOpenStorage()
	if err != nil {
		return err
	}
	defer func() { _ = storage.Close() }()
	instances, _, err := storage.LoadWithGroups()
	if err != nil {
		return err
	}
	for _, inst := range instances {
		if inst.ID != id {
			continue
		}
		ts := inst.GetTmuxSession()
		if ts == nil {
			return errors.New("session has no terminal")
		}
		for _, k := range keys {
			if err := ts.SendNamedKey(k); err != nil {
				return err
			}
		}
		return nil
	}
	return errDeckpadSessionNotFound
}

func (s *Server) handleDeckpadKeys(w http.ResponseWriter, r *http.Request, sessionID string) {
	if r.Method != http.MethodPost {
		writeAPIError(w, http.StatusMethodNotAllowed, ErrCodeMethodNotAllowed, "method not allowed")
		return
	}
	if !s.checkMutationsAllowed(w) {
		return
	}
	if !s.checkMutationRateLimit(w) {
		return
	}
	if !validDeckpadSessionID(sessionID) {
		writeAPIError(w, http.StatusBadRequest, ErrCodeBadRequest, "invalid session id")
		return
	}
	var req deckpadKeysRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4*1024)).Decode(&req); err != nil {
		writeAPIError(w, http.StatusBadRequest, ErrCodeBadRequest, "invalid request body")
		return
	}
	if len(req.Keys) == 0 || len(req.Keys) > deckpadMaxKeysPerRequest {
		writeAPIError(w, http.StatusBadRequest, ErrCodeBadRequest, "keys must hold 1 to 8 entries")
		return
	}
	tmuxKeys := make([]string, 0, len(req.Keys))
	for _, k := range req.Keys {
		name, ok := deckpadKeyNames[k]
		if !ok {
			writeAPIError(w, http.StatusBadRequest, ErrCodeBadRequest, "unknown key: "+k)
			return
		}
		tmuxKeys = append(tmuxKeys, name)
	}
	err := sendDeckpadKeys(s.cfg.Profile, sessionID, tmuxKeys)
	switch {
	case errors.Is(err, errDeckpadSessionNotFound):
		writeAPIError(w, http.StatusNotFound, ErrCodeNotFound, "session not found")
	case err != nil:
		writeAPIError(w, http.StatusBadGateway, ErrCodeInternalError, "could not send keys")
	default:
		writeJSON(w, http.StatusOK, map[string]any{"sent": req.Keys})
	}
}
