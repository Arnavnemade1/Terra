# Working on Terra with other agents

Two people's agents work on this repo at the same time (Vir's and Arnav's). They coordinate through **Axis**
(useaxis.dev), so nobody overwrites anyone else's files and work is not duplicated.

## Connect

- The project is **`terra`**, in the org named in `.axis/axis.json`. `.mcp.json` registers the hosted Axis MCP server
  for Claude Code (`https://useaxis.dev/api/mcp`); the first call opens a browser to sign in (OAuth). Other clients:
  add the same URL as an HTTP MCP server and send `X-Axis-Org` with the org id from `.axis/axis.json`.
- You must be a member of that org. Vir sends invites from useaxis.dev/team.
- Always pass `projectName: "terra"`. Calls without it land in a different, default project.

## Protocol (every session)

1. **Read first.** `get_shared_context` (the notepad), `list_jobs` and `list_locks` for `terra`.
2. **Claim before you build.** Pick a job with `claim_job`, or `post_job` and then claim it. Never start work someone else has claimed.
3. **Lock before you edit.** `propose_file_access` with `filePaths` for every file you will change, and a clear `intent`.
   If you are DENIED, do not edit that file: post on the notepad and pick other work.
4. **Say what you are doing.** One line to `update_shared_context` when you start, when you hit something others
   need to know (an API shape, a broken test, a renamed function), and when you finish.
5. **Finish cleanly.** `complete_job` with an outcome summary, then `release_file_access` for each lock.
   Push your branch; do not push to `main` without the other person agreeing.

Use a stable agent id that says whose it is, e.g. `vir-claude-maptiles` or `arnav-codex-ui`.

## Project rules that matter to every agent

- Every external request goes through `backend/terra/connector.py` (an allowlist). Keys never reach the browser.
- Google Photorealistic 3D Tiles are for display only: nothing may be measured or derived from them
  (Map Tiles API policies), and the Google logo and data attributions must stay visible.
- No infinite or pulsing animations. The UI must work at 360 px wide and up; run the checks in `docs/PHASES.md`.
- Tests: `npm test` (pytest) and `npm run build` (TypeScript and Vite) must pass before you push.
- Data and API terms (Google Map Tiles) are summarised in `docs/COMPLIANCE.md`. Read them before writing anything that ships.
