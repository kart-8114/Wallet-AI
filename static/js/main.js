// Auto-dismiss flash messages after a few seconds
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.flash').forEach((el) => {
    setTimeout(() => { el.style.transition = 'opacity .4s ease'; el.style.opacity = '0'; }, 4000);
  });

  const dropzone = document.getElementById('dropzoneLabel');
  if (dropzone) {
    dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.style.borderColor = 'var(--emerald)'; });
    dropzone.addEventListener('dragleave', () => { dropzone.style.borderColor = ''; });
    dropzone.addEventListener('drop', (e) => {
      e.preventDefault();
      const input = document.getElementById('receipt');
      if (e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        document.getElementById('dropzoneText').textContent = e.dataTransfer.files[0].name;
      }
    });
  }
});
