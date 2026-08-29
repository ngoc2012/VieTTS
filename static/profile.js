document.getElementById('delete-form').addEventListener('submit', (e) => {
    if (!confirm('Delete your account permanently? Remaining balance is lost.')) {
        e.preventDefault();
    }
});
