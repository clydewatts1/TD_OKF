# Project Guidelines

## Generated Artifacts
- Treat okf_bundle/** as generated output, not source of truth.
- Exclude okf_bundle/** from normal code search, analysis, and planning context.
- Only read or edit files in okf_bundle/** when the user explicitly asks for generated output changes.

## Working Directories
- Prefer sandbox/**, README.md, env_sample, and configuration files for implementation changes.
- If guidance in generated markdown conflicts with script logic, trust the script logic and regenerate output.
