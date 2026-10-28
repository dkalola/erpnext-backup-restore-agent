import os
import subprocess
import zipfile
import threading
import time
from flask import Flask, render_template_string, request, Response, jsonify

app = Flask(__name__)
app.secret_key = os.urandom(24)

SITE_NAME = os.environ.get("ERP_SITE_NAME", "frontend")
CONTAINER_NAME = os.environ.get("ERP_CONTAINER_NAME", "frappe_docker-backend-1")
BACKUP_DIR = f"/sites/{SITE_NAME}/private/backups"

# Global state for tracking progress
progress_state = {
    "running": False,
    "percent": 0,
    "status": "Idle",
    "logs": []
}

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>ERPNext Restore Portal</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        #terminal {
            background-color: #1e1e1e;
            color: #00ff00;
            font-family: monospace;
            padding: 15px;
            height: 300px;
            overflow-y: auto;
            border-radius: 5px;
            white-space: pre-wrap;
        }
    </style>
</head>
<body class="bg-light py-5">
    <div class="container" style="max-width: 700px;">
        <div class="card shadow-sm">
            <div class="card-body">
                <h3 class="card-title mb-4 text-center">ERPNext Restore Portal</h3>
                
                <div id="upload-section">
                    <form id="restore-form" enctype="multipart/form-data">
                        <div class="mb-3">
                            <label for="backup_file" class="form-label">Select Master Backup ZIP File</label>
                            <input class="form-control" type="file" id="backup_file" name="backup_file" accept=".zip" required>
                        </div>
                        <div class="alert alert-warning small" role="alert">
                            <strong>Warning:</strong> Restoring will overwrite current data for <code>{{ site_name }}</code>.
                        </div>
                        <button type="submit" id="submit-btn" class="btn btn-danger w-100">Upload & Restore Backup</button>
                    </form>
                </div>

                <div id="progress-section" style="display: none;" class="mt-4">
                    <h5 id="status-text" class="text-muted mb-2">Preparing...</h5>
                    <div class="progress mb-3" style="height: 25px;">
                        <div id="progress-bar" class="progress-bar progress-bar-striped progress-bar-animated bg-success" role="progressbar" style="width: 0%;">0%</div>
                    </div>
                    <div id="terminal"></div>
                    <button id="reset-btn" class="btn btn-secondary w-100 mt-3" style="display: none;" onclick="location.reload()">Back to Upload</button>
                </div>

            </div>
        </div>
    </div>

    <script>
        const form = document.getElementById('restore-form');
        form.onsubmit = async (e) => {
            e.preventDefault();
            const formData = new FormData(form);
            
            document.getElementById('upload-section').style.display = 'none';
            document.getElementById('progress-section').style.display = 'block';

            const response = await fetch('/start-restore', {
                method: 'POST',
                body: formData
            });
            const result = await response.json();
            
            if (!result.success) {
                alert(result.message);
                location.reload();
                return;
            }

            // Start listening to stream logs
            const evtSource = new EventSource("/stream-logs");
            const terminal = document.getElementById('terminal');
            const progressBar = document.getElementById('progress-bar');
            const statusText = document.getElementById('status-text');

            evtSource.onmessage = function(event) {
                const data = JSON.parse(event.data);
                
                progressBar.style.width = data.percent + '%';
                progressBar.innerText = data.percent + '%';
                statusText.innerText = data.status;
                terminal.innerText += data.line + '\\n';
                terminal.scrollTop = terminal.scrollHeight;

                if (!data.running) {
                    evtSource.close();
                    document.getElementById('reset-btn').style.display = 'block';
                    if (data.percent === 100) {
                        statusText.innerText = "Restore Completed Successfully!";
                    } else {
                        statusText.innerText = "Restore Failed / Stopped.";
                        progressBar.classList.remove('bg-success');
                        progressBar.classList.add('bg-danger');
                    }
                }
            };
        };
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, site_name=SITE_NAME)

def run_restore_process(zip_path, sql_file, public_file, private_file):
    global progress_state
    db_password = os.environ.get("DB_ROOT_PASSWORD", "admin")
    
    try:
        progress_state.update({"running": True, "percent": 10, "status": "Extracting backup package...", "logs": []})
        
        sql_path = f"/home/frappe/frappe-bench/sites/{SITE_NAME}/private/backups/{sql_file}"
        pub_path = f"/home/frappe/frappe-bench/sites/{SITE_NAME}/private/backups/{public_file}" if public_file else None
        priv_path = f"/home/frappe/frappe-bench/sites/{SITE_NAME}/private/backups/{private_file}" if private_file else None

        progress_state.update({"percent": 30, "status": "Running database restore..."})
        
        restore_cmd = [
            "docker", "exec", CONTAINER_NAME, 
            "bench", "--site", SITE_NAME, "restore", sql_path,
            "--mariadb-root-password", db_password,
            "--force"
        ]
        if pub_path:
            restore_cmd.extend(["--with-public-files", pub_path])
        if priv_path:
            restore_cmd.extend(["--with-private-files", priv_path])

        process = subprocess.Popen(restore_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        
        for line in process.stdout:
            clean_line = line.strip()
            progress_state["logs"].append(clean_line)
            # Simulated incremental progress during restore
            if progress_state["percent"] < 70:
                progress_state["percent"] += 1

        process.wait()
        if process.returncode != 0:
            progress_state.update({"running": False, "status": "Restore Failed!"})
            return

        progress_state.update({"percent": 80, "status": "Running site migration..."})
        
        migrate_cmd = ["docker", "exec", CONTAINER_NAME, "bench", "--site", SITE_NAME, "migrate"]
        mig_process = subprocess.Popen(migrate_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        
        for line in mig_process.stdout:
            clean_line = line.strip()
            progress_state["logs"].append(clean_line)
            if progress_state["percent"] < 95:
                progress_state["percent"] += 1

        mig_process.wait()
        if mig_process.returncode != 0:
            progress_state.update({"running": False, "status": "Migration Failed!"})
            return

        progress_state.update({"running": False, "percent": 100, "status": "Completed successfully!"})

    except Exception as e:
        progress_state.update({"running": False, "status": f"Error: {str(e)}"})
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)

@app.route("/start-restore", methods=["POST"])
def start_restore():
    if progress_state["running"]:
        return jsonify({"success": False, "message": "A restore process is already running."})
    
    if "backup_file" not in request.files:
        return jsonify({"success": False, "message": "No file uploaded."})
    
    file = request.files["backup_file"]
    if file and file.filename.endswith(".zip"):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        zip_path = os.path.join(BACKUP_DIR, file.filename)
        file.save(zip_path)
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(BACKUP_DIR)
        
        extracted_files = os.listdir(BACKUP_DIR)
        sql_file = next((f for f in extracted_files if f.endswith("-database.sql.gz")), None)
        public_file = next((f for f in extracted_files if f.endswith("-files.tar")), None)
        private_file = next((f for f in extracted_files if f.endswith("-private-files.tar")), None)
        
        if not sql_file:
            return jsonify({"success": False, "message": "Missing database SQL file in zip."})

        # Run process in background thread
        threading.Thread(target=run_restore_process, args=(zip_path, sql_file, public_file, private_file)).start()
        return jsonify({"success": True})

    return jsonify({"success": False, "message": "Invalid file format."})

@app.route("/stream-logs")
def stream_logs():
    def generate():
        last_idx = 0
        while progress_state["running"] or last_idx < len(progress_state["logs"]):
            while last_idx < len(progress_state["logs"]):
                line = progress_state["logs"][last_idx]
                data = {
                    "running": progress_state["running"],
                    "percent": progress_state["percent"],
                    "status": progress_state["status"],
                    "line": line
                }
                import json
                yield f"data: {json.dumps(data)}\n\n"
                last_idx += 1
            time.sleep(0.5)
        # Send final state
        data = {
            "running": progress_state["running"],
            "percent": progress_state["percent"],
            "status": progress_state["status"],
            "line": "Process finished."
        }
        import json
        yield f"data: {json.dumps(data)}\n\n"

    return Response(generate(), mimetype="text/event-stream")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)