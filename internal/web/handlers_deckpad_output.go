package web

import (
	"context"
	"net/http"
	"strings"
)

type deckpadOutputResponse struct {
	SessionID      string `json:"sessionId"`
	Tool           string `json:"tool,omitempty"`
	Role           string `json:"role,omitempty"`
	Content        string `json:"content"`
	Timestamp      string `json:"timestamp,omitempty"`
	ContentVersion string `json:"contentVersion,omitempty"`
	Unchanged      bool   `json:"unchanged"`
	Empty          bool   `json:"empty"`
}

// handleDeckpadOutput implements GET /api/sessions/{id}/output over
// `session output <id> --json [--if-version v]`. A session with no
// response yet is 200 + empty:true so the phone's session screen renders.
func (s *Server) handleDeckpadOutput(w http.ResponseWriter, r *http.Request, sessionID string) {
	if r.Method != http.MethodGet {
		writeAPIError(w, http.StatusMethodNotAllowed, ErrCodeMethodNotAllowed, "method not allowed")
		return
	}
	if !validDeckpadSessionID(sessionID) {
		writeAPIError(w, http.StatusBadRequest, ErrCodeBadRequest, "invalid session id")
		return
	}
	args := []string{"session", "output", sessionID, "--json"}
	if v := strings.TrimSpace(r.URL.Query().Get("ifVersion")); v != "" && validDeckpadSessionID(v) {
		args = append(args, "--if-version", v)
	}
	ctx, cancel := context.WithTimeout(r.Context(), deckpadCLITimeout)
	defer cancel()
	out, code, runErr := runAgentDeckCLI(ctx, s.cfg.Profile, args...)
	m, cliErr := decodeCLIEnvelope(out, code, runErr)
	if cliErr != nil {
		if cliErr.Status == 502 && strings.Contains(strings.ToLower(cliErr.Message), "no response") {
			writeJSON(w, http.StatusOK, deckpadOutputResponse{SessionID: sessionID, Empty: true})
			return
		}
		writeAPIError(w, cliErr.Status, cliErr.Code, cliErr.Message)
		return
	}
	unchanged, _ := m["unchanged"].(bool)
	content := stringField(m, "content")
	writeJSON(w, http.StatusOK, deckpadOutputResponse{
		SessionID:      sessionID,
		Tool:           stringField(m, "tool"),
		Role:           stringField(m, "role"),
		Content:        content,
		Timestamp:      stringField(m, "timestamp"),
		ContentVersion: stringField(m, "content_version"),
		Unchanged:      unchanged,
		Empty:          !unchanged && content == "",
	})
}
