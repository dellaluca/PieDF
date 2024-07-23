async function uploadPDF() {
    const form = document.getElementById('uploadForm');
    const formData = new FormData(form);
    try {
        const response = await fetch('/upload-pdf/', {
            method: 'POST',
            body: formData
        });
        if (!response.ok) {
            throw new Error(`Error: ${response.statusText}`);
        }
        const data = await response.json();
        document.getElementById('filename').value = data.filename;
        loadPDF(data.filename);
    } catch (error) {
        console.error('Error uploading PDF:', error);
        alert('Failed to upload PDF');
    }
}

async function loadPDF(filename) {
    try {
        const response = await fetch(`/download-pdf/${filename}`);
        if (!response.ok) {
            throw new Error(`Error: ${response.statusText}`);
        }
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const pdfViewer = document.getElementById('pdfViewer');
        pdfViewer.innerHTML = `<embed src="${url}" width="100%" height="600px">`;
    } catch (error) {
        console.error('Error loading PDF:', error);
        alert('Failed to load PDF');
    }
}

async function savePDF() {
    const filename = document.getElementById('filename').value;
    const annotations = document.getElementById('annotations').value;
    const formData = new FormData();
    formData.append('filename', filename);
    formData.append('annotations', annotations);
    try {
        const response = await fetch('/save-pdf/', {
            method: 'POST',
            body: formData
        });
        if (!response.ok) {
            throw new Error(`Error: ${response.statusText}`);
        }
        const data = await response.json();
        alert(data.message);
    } catch (error) {
        console.error('Error saving PDF:', error);
        alert('Failed to save PDF');
    }
}
