package tui

// statusline.go documents the status line, which is rendered inline by
// Model.renderStatusLine in app.go. It is split out here to keep the file
// layout aligned with the plan (U16 Files: statusline.go) and to host the
// helpers a richer status line would use.

// StatusData is the snapshot the status line renders: instance id, focused
// session name, token + cost meters, and keybind hints. The live values flow in
// via StatusMsg / SubTabsMsg; this struct documents the contract for callers
// that want to compute a status line outside the Bubble Tea view (e.g. tests).
type StatusData struct {
	Instance string
	Focused  string
	Tokens   int64
	CostUSD  float64
	Drift    int
}
