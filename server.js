// Load environment variables from the .env file
require('dotenv').config();

const express = require('express');
const cors = require('cors');
const { OpenAI } = require('openai');
const { GoogleGenerativeAI } = require('@google/generative-ai');

// --- Initialize AI Clients ---
// Make sure you have set your keys in the .env file
const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });
const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);
const geminiModel = genAI.getGenerativeModel({ model: "gemini-pro" });

// --- Set up the Express App ---
const app = express();
const PORT = process.env.PORT || 3000;

// --- Middleware ---
app.use(cors()); // Allow requests from your frontend
app.use(express.json()); // Allow the server to understand JSON data

// --- API Endpoint for AI Chat ---
// The frontend will send requests to this URL
app.post('/api/chat', async (req, res) => {
    try {
        const { message, model } = req.body; // Get the user's message and chosen model from the request

        if (!message) {
            return res.status(400).json({ error: 'Message is required' });
        }

        let aiResponse = '';

        // --- Logic to choose between OpenAI and Gemini ---
        if (model === 'openai') {
            console.log('Requesting OpenAI...');
            const completion = await openai.chat.completions.create({
                messages: [{ role: "system", content: "You are a helpful Norwegian assistant." }, { role: "user", content: message }],
                model: "gpt-3.5-turbo", // Or "gpt-4" if you have access
            });
            aiResponse = completion.choices[0].message.content;

        } else if (model === 'gemini') {
            console.log('Requesting Gemini...');
            const result = await geminiModel.generateContent(message);
            const response = await result.response;
            aiResponse = response.text();

        } else {
            return res.status(400).json({ error: 'A valid model (openai or gemini) is required' });
        }

        // Send the AI's response back to the frontend
        res.json({ reply: aiResponse });

    } catch (error) {
        console.error('Error processing AI request:', error);
        res.status(500).json({ error: 'Failed to get response from AI' });
    }
});

// --- Start the Server ---
app.listen(PORT, () => {
    console.log(`Server is running on http://localhost:${PORT}`);
});