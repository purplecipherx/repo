# MOM Control: Windows self-host, Railway-free

This branch preserves the former relay source as historical reference; the new deployment uses the ORIGINAL MOM Windows-local MCP server directly. The original local MOM path is `C:\Users\cypher\Desktop\M0J1M0J1_MOM`.

Architecture: `ChatGPT → Secure MCP Tunnel → Windows local MCP (127.0.0.1:8765) → MOM/Foundry`.

## Windows actions

1. Copy `Start-MOM-NoRailway.ps1` and `Check-MOM-NoRailway.ps1` to the Windows computer.
2. Run `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Check-MOM-NoRailway.ps1`.
3. Run `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Start-MOM-NoRailway.ps1`. This starts the pre-existing local MOM launcher, runs its existing secure tunnel bootstrap, and stops only the obsolete `mom_relay_agent.py` processes.
4. In ChatGPT web, create/refresh the MOM Control plugin using the approved tunnel ID (if custom MCP tunnel plugins are available on your plan). Confirm `mom_status` and a read-only desktop call work. The script cannot register the ChatGPT plugin automatically.
5. Revoke all old Railway-era agent/plugin tokens, remove the old Railway plugin, and review scheduled tasks so the outbound Railway agent cannot restart. Do not paste credentials in chat.

## Safety

The local service must bind to `127.0.0.1`; do not expose port `8765` via router/firewall. The port check is only a liveness probe, not MCP protocol acceptance or authentication proof. The switch does not mutate Fish/Jinx files or Foundry data. No live deployment, account authorization, or ChatGPT reconnection has been performed or claimed here.

Alternative standalone package generated in the associated ChatGPT session: `MOM_Control_No_Railway_Windows_Migration.zip`.
