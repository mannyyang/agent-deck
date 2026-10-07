package web

import (
	"net/http"
	"strings"
	"sync"
	"time"
	"unicode"

	"github.com/asheshgoplani/agent-deck/internal/session"
)

// GET /api/deckpad/previews: the first ~200 characters of every active
// session's latest assistant response, for the phone's deck tiles.
// Reading transcripts is the expensive part, so results are cached for
// deckpadPreviewTTL and computed in-process (no CLI spawn).

const (
	deckpadPreviewMaxRunes = 200
	deckpadPreviewTTL      = 8 * time.Second
)

type deckpadPreviewRow struct {
	ID        string
	Content   string
	Timestamp string
}

type deckpadPreview struct {
	Preview   string `json:"preview"`
	Timestamp string `json:"timestamp,omitempty"`
}

type deckpadPreviewsResponse struct {
	GeneratedAt time.Time                 `json:"generatedAt"`
	Previews    map[string]deckpadPreview `json:"previews"`
}

// loadDeckpadPreviews is the seam tests stub. The default opens the
// profile's storage and asks each active instance for its last response
// via the cheap path only (no tmux or disk-scan fallbacks).
var loadDeckpadPreviews = func(profile string) ([]deckpadPreviewRow, error) {
	svc := NewSessionDataService(profile)
	storage, _, err := svc.resolveAndOpenStorage()
	if err != nil {
		return nil, err
	}
	defer func() { _ = storage.Close() }()
	instances, _, err := storage.LoadWithGroups()
	if err != nil {
		return nil, err
	}
	var rows []deckpadPreviewRow
	for _, inst := range session.FilterInstancesByArchive(instances, false) {
		resp, err := inst.GetLastResponse()
		if err != nil || resp == nil {
			continue
		}
		rows = append(rows, deckpadPreviewRow{ID: inst.ID, Content: resp.Content, Timestamp: resp.Timestamp})
	}
	return rows, nil
}

type previewCache struct {
	mu      sync.Mutex
	at      time.Time
	profile string
	body    *deckpadPreviewsResponse
}

var deckpadPreviewCache previewCache

func (c *previewCache) reset() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.at, c.profile, c.body = time.Time{}, "", nil
}

func (c *previewCache) expireForTest(at time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.at = at
}

// get returns the cached body for profile or rebuilds it. The lock is held
// across the rebuild so concurrent phone polls share one transcript scan.
func (c *previewCache) get(profile string) (*deckpadPreviewsResponse, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.body != nil && c.profile == profile && time.Since(c.at) < deckpadPreviewTTL {
		return c.body, nil
	}
	rows, err := loadDeckpadPreviews(profile)
	if err != nil {
		return nil, err
	}
	body := &deckpadPreviewsResponse{GeneratedAt: time.Now().UTC(), Previews: make(map[string]deckpadPreview, len(rows))}
	for _, r := range rows {
		body.Previews[r.ID] = deckpadPreview{Preview: deckpadPreviewText(r.Content), Timestamp: r.Timestamp}
	}
	c.at, c.profile, c.body = time.Now(), profile, body
	return body, nil
}

// deckpadPreviewText collapses whitespace and truncates to the rune budget
// with an ellipsis.
func deckpadPreviewText(s string) string {
	fields := strings.FieldsFunc(s, unicode.IsSpace)
	collapsed := strings.Join(fields, " ")
	runes := []rune(collapsed)
	if len(runes) <= deckpadPreviewMaxRunes {
		return collapsed
	}
	return strings.TrimSpace(string(runes[:deckpadPreviewMaxRunes])) + "…"
}

func (s *Server) handleDeckpadPreviews(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeAPIError(w, http.StatusMethodNotAllowed, ErrCodeMethodNotAllowed, "method not allowed")
		return
	}
	if !s.authorizeRequest(r) {
		writeAPIError(w, http.StatusUnauthorized, ErrCodeUnauthorized, "unauthorized")
		return
	}
	body, err := deckpadPreviewCache.get(s.cfg.Profile)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, ErrCodeInternalError, "failed to load previews")
		return
	}
	writeJSON(w, http.StatusOK, body)
}
