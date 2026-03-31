// QuickHatch Tauri App
// Spawns the Python backend server, then opens the WebView to it.
// Provides a command to launch the AI agent directly from the GUI.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use tauri::{Manager, State};

struct Backend {
    process: Mutex<Option<Child>>,
    port: Mutex<u16>,
    _stdout: Mutex<Option<std::process::ChildStdout>>,
}

/// Get the user's full shell PATH (macOS .app bundles don't inherit it).
fn get_user_path() -> String {
    // Try to get PATH from the user's default shell
    if let Ok(shell) = std::env::var("SHELL") {
        let result = Command::new(&shell)
            .args(["-l", "-c", "echo $PATH"])
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .output();
        if let Ok(output) = result {
            let path = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if !path.is_empty() {
                return path;
            }
        }
    }
    // Fallback: common paths on macOS
    std::env::var("PATH").unwrap_or_else(|_| {
        "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin".to_string()
    })
}

/// Find a Python that has the quickhatch module installed.
fn find_python() -> String {
    let full_path = get_user_path();
    std::env::set_var("PATH", &full_path);

    // Collect all python candidates from PATH
    let mut candidates: Vec<String> = Vec::new();
    for dir in full_path.split(':') {
        for name in &["python3", "python"] {
            let path = format!("{}/{}", dir, name);
            if std::path::Path::new(&path).exists() {
                candidates.push(path);
            }
        }
    }
    // Also try bare names (which may resolve via PATH)
    candidates.push("python3".to_string());
    candidates.push("python".to_string());

    // Find the first one that can import quickhatch
    for candidate in &candidates {
        let result = Command::new(candidate)
            .args(["-c", "import quickhatch"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        if let Ok(status) = result {
            if status.success() {
                return candidate.to_string();
            }
        }
    }

    // Fallback: first working python
    for name in &["python3", "python"] {
        let result = Command::new(name)
            .arg("--version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        if let Ok(status) = result {
            if status.success() {
                return name.to_string();
            }
        }
    }
    "python3".to_string()
}

/// Start the Python backend and return the port and the stdout handle
/// (kept alive so the Python process doesn't get BrokenPipeError).
fn start_backend() -> Result<(Child, u16, std::process::ChildStdout), String> {
    let python = find_python();

    let mut child = Command::new(&python)
        .args(["-m", "quickhatch", "--gui", "--no-browser", "--tauri", "--port", "0"])
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .map_err(|e| format!("Failed to start Python backend: {}", e))?;

    // Read stdout line by line looking for the port
    let stdout = child.stdout.take().ok_or("No stdout")?;
    let mut reader = BufReader::new(stdout);
    let mut port: u16 = 0;
    let mut line_buf = String::new();

    loop {
        line_buf.clear();
        match reader.read_line(&mut line_buf) {
            Ok(0) => break, // EOF
            Ok(_) => {
                if let Some(p) = line_buf.strip_prefix("QUICKHATCH_PORT=") {
                    if let Ok(parsed) = p.trim().parse::<u16>() {
                        port = parsed;
                        break;
                    }
                }
            }
            Err(_) => break,
        }
    }

    if port == 0 {
        let _ = child.kill();
        return Err("Backend did not report its port".to_string());
    }

    // Return the stdout handle so it stays alive (prevents BrokenPipeError in Python)
    Ok((child, port, reader.into_inner()))
}

#[tauri::command]
fn get_backend_url(state: State<Backend>) -> String {
    let port = state.port.lock().unwrap();
    format!("http://localhost:{}", *port)
}

#[tauri::command]
fn launch_agent(state: State<Backend>, provider: String) -> Result<String, String> {
    let port = *state.port.lock().unwrap();
    let base = format!("http://localhost:{}", port);

    let prompt = format!(
        "Read {base}/api/profile for my system info. \
         Use the QuickHatch bridge API at {base}/api to communicate. \
         POST /api/suggest for recommendations, POST /api/ask for questions, \
         POST /api/done when finished."
    );

    // Write prompt to temp file for reliable passing
    let prompt_file = std::env::temp_dir().join("quickhatch-agent-prompt.txt");
    std::fs::write(&prompt_file, &prompt)
        .map_err(|e| format!("Failed to write prompt file: {}", e))?;

    #[cfg(target_os = "windows")]
    {
        let (cmd, args) = match provider.as_str() {
            "claude_code" => ("claude", vec![
                "-p".to_string(),
                prompt.clone(),
                "--dangerously-skip-permissions".to_string(),
            ]),
            "codex" => ("codex", vec![prompt.clone()]),
            _ => {
                return Err(format!(
                    "Open your AI tool and point it at {}/api/profile", base
                ));
            }
        };

        // Spawn in a new terminal window so the user can see it
        Command::new("cmd")
            .args(["/c", "start", "", cmd])
            .args(&args)
            .spawn()
            .map_err(|e| format!("Failed to launch {}: {}. Is it installed?", cmd, e))?;
    }

    #[cfg(not(target_os = "windows"))]
    {
        let prompt_path = prompt_file.to_string_lossy().to_string();
        let shell_cmd = match provider.as_str() {
            "claude_code" => format!(
                "claude -p \"$(cat {})\" --dangerously-skip-permissions",
                prompt_path
            ),
            "codex" => format!("codex \"$(cat {})\"", prompt_path),
            _ => {
                return Err(format!(
                    "Open your AI tool and point it at {}/api/profile", base
                ));
            }
        };

        // Try common terminal emulators
        let terminals = [
            ("x-terminal-emulator", vec!["-e"]),
            ("gnome-terminal", vec!["--"]),
            ("konsole", vec!["-e"]),
            ("xfce4-terminal", vec!["-e"]),
            ("xterm", vec!["-e"]),
        ];

        let mut launched = false;
        for (term, prefix_args) in &terminals {
            let mut args: Vec<&str> = prefix_args.iter().map(|s| &**s).collect();
            args.push("bash");
            args.push("-c");
            // We need to pass the shell_cmd as a single arg
            let result = Command::new(term)
                .args(&args)
                .arg(&shell_cmd)
                .spawn();
            if result.is_ok() {
                launched = true;
                break;
            }
        }

        if !launched {
            // Fallback: just spawn directly without a terminal
            Command::new("bash")
                .args(["-c", &shell_cmd])
                .spawn()
                .map_err(|e| format!("Failed to launch agent: {}", e))?;
        }
    }

    Ok("Agent launched! Check the new terminal window.".to_string())
}

fn main() {
    // Start the Python backend before Tauri
    let (child, port, stdout_handle) = match start_backend() {
        Ok(result) => result,
        Err(e) => {
            eprintln!("Error: {}", e);
            eprintln!("Make sure QuickHatch is installed: pip install -e .");
            std::process::exit(1);
        }
    };

    let backend = Backend {
        process: Mutex::new(Some(child)),
        port: Mutex::new(port),
        _stdout: Mutex::new(Some(stdout_handle)),
    };

    let url = format!("http://localhost:{}", port);

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(backend)
        .setup(move |app| {
            // Navigate the main window to the Python backend
            let window = app.get_webview_window("main")
                .expect("main window not found");
            let parsed_url = url.parse()
                .map_err(|e| format!("Invalid URL {}: {}", url, e))?;
            window.navigate(parsed_url)?;
            window.show()?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![get_backend_url, launch_agent])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                // Kill the Python backend when the window closes
                let app = window.app_handle();
                let state: State<Backend> = app.state();
                let mut proc = state.process.lock().unwrap();
                if let Some(ref mut child) = *proc {
                    let _ = child.kill();
                }
                drop(proc);
            }
        })
        .run(tauri::generate_context!())
        .expect("error running QuickHatch");
}
