const amountInput = document.getElementById('amount');
const msg = document.getElementById('paypal-msg');

paypal.Buttons({
    createOrder: async () => {
        const res = await fetch('/api/paypal/create_order', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ amount: parseFloat(amountInput.value) })
        });
        const data = await res.json();
        if (!res.ok || !data.id) throw new Error(data.error || 'Order creation failed');
        return data.id;
    },
    onApprove: async (data) => {
        const res = await fetch('/api/paypal/capture_order', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ orderID: data.orderID })
        });
        const result = await res.json();
        if (!res.ok || !result.ok) {
            msg.textContent = 'Payment failed: ' + (result.error || 'Unknown error');
            return;
        }
        msg.textContent = `✓ Credited ${(result.credited_cents / 100).toFixed(2)} € — reloading...`;
        setTimeout(() => location.reload(), 1200);
    },
    onError: (err) => {
        msg.textContent = 'PayPal error: ' + err.message;
    }
}).render('#paypal-buttons');
