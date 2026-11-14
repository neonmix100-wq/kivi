# Durable approvals and PermissionEngine

`kivi memory approvals grant` writes a durable `(task_id, action, scope)` row.
`ToolExecutor` now checks that store before asking for interactive confirmation:

```text
R2/R3 tool call
  → PermissionEngine: confirm required
  → ToolExecutor: is there a live durable approval for task_id + tool (+ scope)?
       yes → run with approved=True
       no  → return confirmation required
```

Examples:

```powershell
kivi memory approvals grant task-123 shell.exec --ttl 600
kivi memory approvals grant task-123 crm.fill --scope "https://gs-leads.ru/" --ttl 300
kivi memory approvals list
kivi memory approvals revoke 4
```

Expired and revoked approvals never match. A broad empty scope covers all
paths; a non-empty scope narrows the approval to that concrete target.
