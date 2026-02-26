# Suggested UI Improvements

1. **Introduce a two-pane workflow (library browser + metadata editor).**
   Keep file browsing on the left and editable tags on the right so users can move quickly between tracks without losing context. Add sticky save/discard controls in the editor pane to reduce accidental navigation loss.

2. **Add an explicit "match confidence" badge for lookup results.**
   Display a clear score label (e.g., High/Medium/Low with numeric score) on Discogs/Beatport/Juno candidates and highlight why a match scored well (artist/title/label/year). This makes lookup decisions faster and reduces wrong tag writes.

3. **Improve form ergonomics with grouped fields and progressive disclosure.**
   Group core tags first (Title, Artist, Album, Track, Genre, Year), and collapse advanced fields (MusicBrainz TXXX, comments, publisher, BPM) under an "Advanced" section. This keeps common edits fast while preserving power-user access.

4. **Add keyboard-first editing shortcuts and bulk actions.**
   Support shortcuts such as `Ctrl/Cmd+S` (save), `J/K` (next/previous file), and `Ctrl/Cmd+Enter` (apply selected lookup result). Add multi-select + "apply genre/year/label to selected" for folder-level cleanup to speed repetitive tasks.

5. **Strengthen status feedback with non-blocking toasts and inline validation.**
   Show toast notifications for saves/lookups/cover-art updates and inline warnings for invalid paths, unsupported files, or missing required tags. Pair this with an undo-after-save window (or lightweight history panel) to improve confidence and recoverability.
