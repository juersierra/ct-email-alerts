# ct-email-alerts

Chequeo diario del spam rate de los dominios de envío de Central Ticket
(`notificaciones.centralticket.net` y `marketing.centralticket.net`).

Lee Google Postmaster Tools (spam rate de Gmail y reputación) y Mailgun (tasa de quejas).
Si algún umbral se supera, manda una alerta por Mailgun a los destinatarios de `config.json`.
Si no hay nada que reportar, no manda nada.

## Reglas

| Regla | Umbral (`config.json`) |
|---|---|
| Spam rate Gmail, alerta | ≥ 0,1% |
| Spam rate Gmail, crítica | ≥ 0,3% (Gmail empieza a penalizar) |
| Suba brusca | último día > 2× promedio de los 7 previos, y ≥ 0,05% |
| Reputación Postmaster | LOW o BAD |
| Quejas Mailgun | > 0,08% con al menos 100 entregados en el día |
| Postmaster sin datos | más de 4 días sin datos |

Los datos de Gmail llegan con 1-2 días de demora. Mailgun no ve las quejas de Gmail.

## Credenciales

Se configuran en el entorno en la nube **Central Ticket**, nunca en este repo:

- Mailgun: network secret para `api.mailgun.net` (Basic, usuario `api`).
- Google: variables `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`.
- Red: `oauth2.googleapis.com` y `gmailpostmastertools.googleapis.com` permitidos.

## Uso

```bash
python3 check_spam.py              # chequea; envía email solo si hay alertas
python3 check_spam.py --dry-run    # no envía nada
python3 check_spam.py --test-alert # fuerza un email de prueba
```

Código de salida: 0 sin alertas, 1 con alertas, 2 error de fuentes.
