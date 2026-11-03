# Privacy Policy

_Last updated: 2026-11-02_

This policy covers `print-to-thermal` and the `thermal-printer` Claude Code
plugin (https://github.com/matmar10/thermal).

## We collect nothing

There is no account, no telemetry, no analytics, no crash reporting and no
server run by us. We do not collect, receive, store, sell or share any data
about you or what you print.

## Where your data goes

What you print is sent only to the printer **you** configure: a network
address such as `tcp://192.168.1.50:9100`, a CUPS or Windows printer queue,
or a USB/serial device on your computer. It is sent nowhere else.

The only other network traffic is:

- **`print-to-thermal discover --scan`**, only if you run it. It checks your
  own local network for devices listening on port 9100 (the usual printer
  port). It reports what it finds to you only.
- **`print-to-thermal serve`**, only if you run it. It answers HTTP requests
  on the address you choose, and you can limit it with `--token` and
  `--allow`. It forwards jobs only to your configured printer.

## What stays on your computer

Your printer settings, paper rolls and templates are saved in
`~/.config/thermal/` (or `$THERMAL_HOME`). These files stay on your
computer, and you can delete them at any time.

## Claude

When you ask Claude to print something, your conversation, including what
you print, is handled by Anthropic under its own terms and privacy policy.
This project does not receive any of it.

## Contact

Questions or concerns: open an issue at
https://github.com/matmar10/thermal/issues.
