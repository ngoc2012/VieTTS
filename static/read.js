const form = document.getElementById('upload-form');
const input = document.getElementById('pdf-input');
const dropArea = document.getElementById('drop-area');
const fileNameDisplay = document.getElementById('file-name-display');
const statusMsg = document.getElementById('status-msg');
const loader = document.getElementById('loader');
const submitBtn = document.getElementById('submit-btn');

input.addEventListener('change', () => {
    if (input.files.length > 0) {
        fileNameDisplay.textContent = input.files[0].name;
        fileNameDisplay.style.color = '#00f2ff';
    }
});

form.addEventListener('submit', async (e) => {
    e.preventDefault();

    const file = input.files[0];
    if (!file) return;

    const formData = new FormData();
    formData.append('file', file);

    // UI Reset
    statusMsg.textContent = 'Uploading and converting...';
    statusMsg.className = 'status info';
    loader.style.display = 'block';
    submitBtn.disabled = true;

    try {
        const response = await fetch('/api/upload_pdf', {
            method: 'POST',
            body: formData
        });

        const data = await response.json();

        if (data.ok) {
            statusMsg.innerHTML = `✓ Converted ${data.images.length} pages successfully!<br><br><a href="/viewer/${data.pdf_id}" class="btn-primary" style="display: inline-block; text-decoration: none; padding: 0.5rem 1.1rem; border-radius: 6px;">Open in Viewer</a>`;
            statusMsg.className = 'status success';

            // Refresh page after 1 second to show in recent uploads
            setTimeout(() => location.reload(), 1000);
        } else {
            statusMsg.textContent = 'Error: ' + data.error;
            statusMsg.className = 'status error';
        }
    } catch (err) {
        statusMsg.textContent = 'Failed to connect to server.';
        statusMsg.className = 'status error';
    } finally {
        loader.style.display = 'none';
        submitBtn.disabled = false;
    }
});

// Simple Drag & Drop visual feedback
['dragenter', 'dragover'].forEach(name => {
    dropArea.addEventListener(name, () => dropArea.style.borderColor = '#bc13fe', false);
});
['dragleave', 'drop'].forEach(name => {
    dropArea.addEventListener(name, () => dropArea.style.borderColor = 'rgba(255, 255, 255, 0.2)', false);
});

// Delete PDF functionality
document.querySelectorAll('.delete-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
        e.preventDefault();
        e.stopPropagation();

        const container = btn.closest('.upload-item-container');
        const pdfId = container.dataset.id;
        const pdfName = container.querySelector('.upload-item-name')?.textContent || 'PDF';

        if (confirm(`Delete "${pdfName}"? This action cannot be undone.`)) {
            try {
                const response = await fetch(`/api/pdf/${pdfId}`, {
                    method: 'DELETE'
                });
                const data = await response.json();
                if (data.ok) {
                    // Remove from UI
                    container.style.opacity = '0';
                    container.style.transition = 'opacity 0.3s';
                    setTimeout(() => container.remove(), 300);
                } else {
                    alert('Failed to delete: ' + (data.error || 'Unknown error'));
                }
            } catch (err) {
                alert('Error deleting PDF: ' + err.message);
            }
        }
    });
});
