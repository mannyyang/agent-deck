package web

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
)

type deckpadSendRequest struct {
	Text  string `json:"text"`
	Enter *bool  `json:"enter,omitempty"`
}

type deckpadSendResponse struct {
	SendID  string `json:"sendId"`
	State   string `json:"state"`
	Verdict string `json:"verdict"`
}

// handleDeckpadSend implements POST /api/sessions/{id}/send: queue text for
// a session through `session send --queue --json`. 202 with the send id.
func (s *Server) handleDeckpadSend(w http.ResponseWriter, r *http.Request, sessionID string) {
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
	var req deckpadSendRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64*1024)).Decode(&req); err != nil {
		writeAPIError(w, http.StatusBadRequest, ErrCodeBadRequest, "invalid request body")
		return
	}
	text := strings.TrimSpace(req.Text)
	if text == "" {
		writeAPIError(w, http.StatusBadRequest, ErrCodeBadRequest, "text is required")
		return
	}
	// `session send` submits the message; enter=false asks for a draft
	// (pre-filled, not submitted), which the CLI exposes as --draft and
	// which is incompatible with --queue.
	args := []string{"session", "send", sessionID, text, "--queue", "--json"}
	if req.Enter != nil && !*req.Enter {
		args = []string{"session", "send", sessionID, text, "--draft", "--json"}
	}
	ctx, cancel := context.WithTimeout(r.Context(), deckpadCLITimeout)
	defer cancel()
	out, code, runErr := runAgentDeckCLI(ctx, s.cfg.Profile, args...)
	m, cliErr := decodeCLIEnvelope(out, code, runErr)
	if cliErr != nil {
		writeAPIError(w, cliErr.Status, cliErr.Code, cliErr.Message)
		return
	}
	writeJSON(w, http.StatusAccepted, deckpadSendResponse{
		SendID:  stringField(m, "send_id"),
		State:   stringField(m, "state"),
		Verdict: stringField(m, "verdict"),
	})
}
