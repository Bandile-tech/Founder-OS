// Persistent session
let sessionId = localStorage.getItem("sessionId");
if (!sessionId) {
    sessionId = Date.now().toString(36) + Math.random().toString(36).substring(2);
    localStorage.setItem("sessionId", sessionId);
}

const chatBox = document.getElementById("chat-box");
const inputField = document.getElementById("user-input");
const chatForm = document.getElementById("chat-form");

// Load chat history from localStorage if available
let chatHistory = JSON.parse(localStorage.getItem("chatHistory")) || [];

// Render existing chat messages on page load
chatHistory.forEach(item => addMessageToDOM(item.message, item.sender));

function addMessageToDOM(message, sender) {
    const div = document.createElement("div");
    div.className = `message ${sender}`;
    div.innerHTML = message.replace(/\n/g, "<br>");
    chatBox.appendChild(div);
    chatBox.scrollTop = chatBox.scrollHeight;
}

// Add a message to memory + DOM + localStorage
function addMessage(message, sender) {
    chatHistory.push({ sender, message });
    localStorage.setItem("chatHistory", JSON.stringify(chatHistory));
    addMessageToDOM(message, sender);
}

// Send message to backend
async function sendMessage() {
    const message = inputField.value.trim();
    if (!message) return;
    inputField.value = "";

    addMessage(message, "user"); // Add user message

    // Typing indicator
    const typingIndicator = document.createElement("div");
    typingIndicator.className = "message bot";
    typingIndicator.innerHTML = "AI is thinking...";
    chatBox.appendChild(typingIndicator);
    chatBox.scrollTop = chatBox.scrollHeight;

    try {
        const response = await fetch("http://127.0.0.1:8000/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message, session_id: sessionId })
        });
        const data = await response.json();
        typingIndicator.remove();
        addMessage(data.reply, "bot"); // Add bot message
    } catch (err) {
        typingIndicator.remove();
        addMessage("Error connecting to backend. Message preserved.", "bot");
        console.error(err);
    }
}

// Handle form submission (Enter key + Send button)
chatForm.addEventListener("submit", function(event) {
    event.preventDefault(); // Prevent page reload
    sendMessage();
});
