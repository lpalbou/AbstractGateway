"""Per-user email on the gateway (framework backlog 0992 WP3).

One user -> one runtime -> one mailbox. Every piece resolves from the principal's own data
plane and never from a path or body id:

- `accounts`       the plane's `EmailAccountStore` (AbstractCore's one mail implementation),
                   the admin's per-user email switch, the bring-your-own OAuth clients, the
                   one-time import of the retired `ABSTRACT_EMAIL_*` configuration;
- `watcher`        the read-only mail watcher: durable cursor, UIDVALIDITY resync, a durable
                   per-plane inbox the `email.received` trigger reads;
- `notifications`  notification preferences, the durable outbox and the fixed templates;
- `recovery`       "Forgot your token?" and "Email me a sign-in code" for users with email;
- `worker`         the per-plane thread that runs the watcher and the dispatcher;
- `audit`          typed account / send / recovery events in the gateway audit log.

The gateway never talks IMAP/SMTP itself: every connection goes through
`abstractcore.comms.email` (verified TLS, typed errors, recipient policy, send limits).
"""
