# Gmail Agent Skills

## Gmail Query Tips

- Unread inbox: `is:unread in:inbox`
- Recent non-trash messages: `newer_than:7d -in:trash`
- Sender search: `from:person@example.com`
- Subject search: `subject:(invoice)`
- Attachments: `has:attachment`
- Important: `is:important`

## Triage Categories

- `urgent`: time-sensitive, high-impact, or from a critical person.
- `needs_reply`: user likely needs to respond.
- `needs_review`: worth reading but not necessarily urgent.
- `read_later`: useful but not immediate.
- `notification`: automated update, receipt, alert, or status.
- `spam_or_noise`: low-value promotional/bulk/noise that should usually not surface.

## Prefilter Discipline

The prefilter is a learned skip-list, not a spam detector. It exists to save tokens after a sender/domain has already been classified as recurring noise.
