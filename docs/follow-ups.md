# Follow-ups

Open items to pick up next, most important first.

## 1. Bill replies show raw Markdown symbols

**Problem.** Chat bubbles show plain text (`white-space: pre-wrap`), but the live model formats its
replies with Markdown. Customers see the raw symbols. A real reply from the live demo:

```
Here are your bills:

- **September 2026:** R$189.51 — due Oct 28, 2026 — **open**
- **August 2026:** R$190.40 — due Sep 28, 2026 — paid
- **July 2026:** R$218.71 — due Aug 28, 2026 — paid
```

Earlier replies also contained `| --- |` table rows. This is not user friendly: the goal is a reply a
non-technical customer reads at a glance.

**Options.**

- **Render a safe Markdown subset in the bubble** (bold, lists, simple tables) with a small
  sanitising renderer. The model's formatting then looks the way it was meant to look. Needs care:
  model text must never become raw HTML.
- **Show bill lists as cards or a table built by the page** from the tool data, with status badges
  (open, overdue, paid), so the layout no longer depends on the model's formatting.
- **Plain-text only.** One rule in `services/support_api/prompts/system_en.md` ("plain text, no
  Markdown; simple `- ` lists"). The quickest fix, but the most basic look.

Decide which before building. Whatever is chosen should look the same in offline (scripted) mode.

## 2. Codespaces rewrites the browser's Origin header

In a Codespace, chat requests reach `support-api` with `Origin: https://localhost:5173`, not the
Codespace URL. `scripts/init-env.sh` writes only the Codespace URL to `ALLOWED_ORIGINS`, so a fresh
`.env` makes every chat message fail with 403 `origin_not_allowed`. Add `https://localhost:5173` (and
`http://localhost:5173`) to the generated list.
