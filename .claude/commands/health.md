Run both health and security checks on the macro-ai-digest app:

```
uv run digest health
uv run digest security
```

Then give a concise status report:
- One-line overall verdict (healthy / degraded / critical)
- Bullet list of any failures or warnings with specific remediation steps
- Skip components that are fully green — only surface what needs attention
- If everything is green, say so in one line

Keep the total response under 15 lines.
