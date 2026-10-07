package web

import (
	"context"
	"net/http"
)

type deckpadSendStatusResponse struct {
	SendID  string `json:"sendId"`
	State   string `json:"state"`
	Verdict string `json:"verdict"`
	Reason  string `json:"reason,omitempty"`
	Settled bool   `json:"settled"`
}

// handleDeckpadSendStatus implements GET /api/sessions/{id}/send-status/{sendId}
// over `session send-status <id> --json`. Read-only safe.
func (s *Server) handleDeckpadSendStatus(w http.ResponseWriter, r *http.Request, sessionID, sendID string) {
	if r.Method != http.MethodGet {
		writeAPIError(w, http.StatusMethodNotAllowed, ErrCodeMethodNotAllowed, "method not allowed")
		return
	}
	if !validDeckpadSessionID(sessionID) || !validDeckpadSessionID(sendID) {
		writeAPIError(w, http.StatusBadRequest, ErrCodeBadRequest, "invalid id")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), deckpadCLITimeout)
	defer cancel()
	out, code, runErr := runAgentDeckCLI(ctx, s.cfg.Profile, "session", "send-status", sendID, "--json")
	m, cliErr := decodeCLIEnvelope(out, code, runErr)
	if cliErr != nil {
		writeAPIError(w, cliErr.Status, cliErr.Code, cliErr.Message)
		return
	}
	settled, _ := m["settled"].(bool)
	writeJSON(w, http.StatusOK, deckpadSendStatusResponse{
		SendID:  stringField(m, "send_id"),
		State:   stringField(m, "state"),
		Verdict: stringField(m, "verdict"),
		Reason:  stringField(m, "reason"),
		Settled: settled,
	})
}
