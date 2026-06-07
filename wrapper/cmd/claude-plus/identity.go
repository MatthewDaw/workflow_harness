package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// wrapperIdentity is the signed-in identity shown in the status line, decoded
// from the locally stored device token. It is DISPLAY-ONLY — the token's
// signature is not verified here (HQ verifies it on every request); we only read
// the public payload to label the session.
type wrapperIdentity struct {
	Name string // approver's display name (token `name` claim); may be empty
	Org  string // org the login was bound to (token `org` claim)
}

// loadIdentity reads ~/.claude-plus/credentials (line 2 = device token) and
// decodes the JWT payload to recover the signed-in name + org. Returns ok=false
// when there is no credentials file / token — i.e. the user is not logged in.
func loadIdentity() (id wrapperIdentity, ok bool) {
	home, err := os.UserHomeDir()
	if err != nil {
		return wrapperIdentity{}, false
	}
	raw, err := os.ReadFile(filepath.Join(home, ".claude-plus", "credentials"))
	if err != nil {
		return wrapperIdentity{}, false
	}
	lines := strings.Split(string(raw), "\n")
	if len(lines) < 2 {
		return wrapperIdentity{}, false
	}
	token := strings.TrimSpace(lines[1])
	if token == "" {
		return wrapperIdentity{}, false
	}
	claims, err := decodeTokenClaims(token)
	if err != nil || claims.Org == "" {
		return wrapperIdentity{}, false
	}
	return wrapperIdentity{Name: claims.Name, Org: claims.Org}, true
}

type tokenClaims struct {
	Org  string `json:"org"`
	Name string `json:"name"`
	Sub  string `json:"sub"`
}

// decodeTokenClaims base64url-decodes a JWT's payload segment WITHOUT verifying
// the signature. The token is the user's own and is used only to label the UI;
// HQ remains the sole authority on token validity.
func decodeTokenClaims(token string) (tokenClaims, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return tokenClaims{}, fmt.Errorf("malformed token")
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return tokenClaims{}, err
	}
	var c tokenClaims
	if err := json.Unmarshal(payload, &c); err != nil {
		return tokenClaims{}, err
	}
	return c, nil
}
