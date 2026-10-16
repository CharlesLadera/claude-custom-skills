# Label convention

The single owner of the gmail-organize label format. `SKILL.md` and the state
file follow this file; change it here and nowhere else.

## Two layers

Every thread gets **at most one topic label** and **at most one action label**.

### Action layer — what you owe

| Label | Meaning | Color (background / text) |
|---|---|---|
| `@Action` | you owe a reply or a task | `#fb4c2f` / `#ffffff` |
| `@Waiting` | someone else owes you | `#fad165` / `#000000` |
| `@Read` | read later, no action | `#4a86e8` / `#ffffff` |

Applied by the user, by hand. The skill creates these three labels and reports
stale ones; it never applies them in bulk.

### Topic layer — what it is about

Fixed roots. Leaves are created as needed.

| Root | Holds | Inbox |
|---|---|---|
| `Clients/<Org>` | client correspondence | stays |
| `Team` | colleagues at the mailbox's own domain | stays |
| `Projects/<Name>` | internal project or automation threads | stays |
| `Vendors/<Org>` | tools and suppliers you deal with as a person | stays |
| `Finance/Receipts` | receipts, order confirmations, payment notices | archived |
| `Finance/Invoices` | invoices to pay or track | stays |
| `Notify/<Source>[/<Sub>]` | machine notifications and alerts | archived |
| `News` | newsletters, digests, marketing lists | archived |
| `Personal` | non-work | stays |

"Archived" means rules and filters for that label set `archive: true`
(remove `INBOX`).

## Naming rules

- Hierarchy only through `/`; at most 3 levels (`Notify/GitHub/<repo>`).
- Leaf names in Title Case, using the product's own casing for sources
  (`GitHub`, `Jira`, `Slack`, `Vercel`, `Google`).
- No `|`, `-`, `:` or other separators standing in for hierarchy.
- No dates in names — a dated label is a temp label and gets removed.
- Never modify Gmail system labels or categories (`CATEGORY_*`); read them as hints.

## Rule format (state file)

```json
{"match": {"list_id": "repo.github.com"}, "label": "Notify/GitHub", "archive": true}
```

`match` has exactly one key:

| Key | Matches summary group | Filter criteria |
|---|---|---|
| `list_id` | `key == "list:<value>"` | `{"query": "list:<value>"}` |
| `from` | `kind == "from"` and `from == <value>` | `{"from": "<value>"}` |
| `domain` | `domain == <value>` | `{"from": "<value>"}` |

Most specific wins: `list_id` > `from` > `domain`.

## Classifying a sender group

Work from the summary group's fields. First match wins; when two rows fit
equally, ask.

1. A state rule matches → use it.
2. `from` is the mailbox's own domain and the sender is a person → `Team`.
3. Automated sender (`noreply`, `no-reply`, `notifications`, `alerts`, `bot`,
   `mailer-daemon` in the local part, or a known product domain) → `Notify/<Source>`,
   archived. Per-repo or per-board sublabels only when the source already has
   more than ~50 messages a month.
4. Subjects about receipts, orders, payments received → `Finance/Receipts`;
   invoices or bills to pay → `Finance/Invoices`.
5. `list_mail` > half of `count`, or mostly `CATEGORY_PROMOTIONS` → `News`, archived.
6. A person at an external domain with `has_sent_in_thread > 0` → `Clients/<Org>`
   or `Vendors/<Org>` — **always ask which, and the org name**; never guess a
   client.
7. Free-mail domains (`gmail.com`, `yahoo.com`, `outlook.com`, …) with personal
   subjects → `Personal`; ask if unsure.
8. Anything else → `?` in the table.

Existing labels in `label_ids` are evidence: if most of a group already sits
under a label that migrates to X, propose X.

## Trash eligibility

A group may appear in the trash table only if it would classify as `News`,
`Notify/*`, or is mostly `CATEGORY_PROMOTIONS`, **and** its `oldest` date is
older than `trash_min_age_days`. Never propose `Clients`, `Team`, `Finance`,
`Personal`, or any group with `has_sent_in_thread > 0`. `apply.py` re-checks
every message regardless (starred, important, protected labels, age, sent
threads) and skips the ones that fail.
