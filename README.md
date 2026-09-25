# Jinnah Metadata Generator

**Jinnah Metadata Generator** is a Python desktop application for generating and organizing stock-design metadata for **Adobe Stock, Shutterstock, and Vecteezy** using the **Google Gemini API**.

The application provides a Tkinter-based graphical interface and a processing engine designed for batch image processing, metadata generation, CSV export, quality checking, API rate-limit handling, and crash/resume support.

---

## ✨ Features

### 🤖 AI Metadata Generation

The application uses the Google Gemini API to generate metadata for stock designs, including:

* Title
* Description
* Category
* Keywords
* People/Property

The metadata generation prompt requests **45–50 relevant keywords** and avoids generic stock filler keywords such as `image`, `photo`, `design`, `vector`, `illustration`, `background`, and `stock`.

---

### 🏢 Supported Stock Platforms

The application currently supports:

* **Adobe Stock**
* **Shutterstock**
* **Vecteezy**

These platforms are built into the CSV generation system.

---

### 📄 Automatic CSV Generation

The software generates CSV metadata files for the supported platforms.

Default CSV files:

```text
adobe_stock.csv
shutterstock.csv
vecteezy.csv
```

It also generates:

```text
qa_score.csv
failed_queue.csv
engine.log
engine_state.json
progress_status.json
fallback_id_map.json
```

when the corresponding workflow/state requires them.

---

### 📊 QA Score

The engine calculates a metadata quality score based on several checks, including:

* Title readability
* Keyword count
* Keyword diversity
* Category validity
* Banned keyword detection
* Duplicate keyword checks

The resulting score is stored in `qa_score.csv`.

---

### 🔍 Image Preflight Validation

Before an image is sent for AI processing, the application checks whether the image exists and can be decoded.

Corrupted or unreadable images can be rejected before an API request is made.

Supported input image extensions in the workflow include:

```text
.jpg
.jpeg
.png
```

---

### 📦 EPS Detection

The application checks whether a matching `.eps` file exists next to the image.

A missing EPS file is **reported but does not stop processing**.

Example:

```text
design01.jpg
design01.eps
```

---

### 🔢 Design Number & CSV Row Alignment

The application keeps CSV rows aligned with the design number contained in the filename.

For example:

```text
1.jpg   → Row 2
50.jpg  → Row 51
100.jpg → Row 101
```

The same design-number alignment is maintained across:

* Adobe Stock CSV
* Shutterstock CSV
* Vecteezy CSV
* QA Score CSV

For filenames without numbers, the application creates and stores a persistent fallback ID mapping.

---

### 🔄 Resume / Progress Recovery

The engine stores processing state and progress information so that previously processed files can be detected and skipped.

The application maintains information such as:

* Completed designs
* Failed jobs
* Current progress
* Last update time
* Processing state

---

### 🔴 Failed Jobs Queue

Failed jobs are stored in:

```text
failed_queue.csv
```

The GUI provides:

```text
🔁 Retry All Failed Jobs
```

to clear the permanent-failure records and allow those files to be processed again.

---

### 🔑 Multi-API Support

The application supports multiple Gemini API keys.

The API manager provides:

* Multiple API key configuration
* API key validation
* Sequential API rotation
* Rate-limit tracking
* 429 handling
* Cooldown management
* Request tracking
* Duplicate API-key protection

Only enabled and available API keys are used.

API keys can also be resolved from environment variables such as:

```text
GEMINI_API_KEY_API_1
```

instead of storing the key directly in the configuration file.

---

### ⏱️ Rate-Limit & 429 Handling

When a Gemini API key receives a genuine `429` response, the engine places that key into a cooldown period and can rotate to another available API key.

The system also prevents repeated late `429` responses from unnecessarily extending an existing cooldown.

---

### ⚙️ Configurable Settings

The application stores configuration in:

```text
config.json
```

Current configuration includes:

* Gemini model
* Maximum requests per minute
* Maximum retries
* Theme
* Auto backup
* Keyword blacklist
* Minimum keywords
* Target keywords
* CSV headers
* Vecteezy license
* API keys

---

### 📋 Custom CSV Headers

The GUI allows CSV columns to be customized.

Available actions include:

* Add Column
* Rename Column
* Delete Column
* Move Up
* Move Down
* Reset to Default

The system validates duplicate and empty column names.

---

### 🎨 Dark & Light Theme

The application includes:

* Dark theme
* Light theme

The selected theme is stored in the configuration and applied to the GUI.

---

### 🖼️ Image Preview

The GUI provides an image preview panel with information including:

* Preview image
* EPS existence
* JPG existence
* SHA256
* Resolution
* File size

---

### 🔐 SHA256 File Hash

The application can calculate a SHA256 hash for image files.

This is used by the GUI and processing system for file identification/information.

---

## 🛠️ Technologies

This project is built with:

* Python
* Tkinter
* Google Gemini API
* Google GenAI Python SDK
* Pillow (PIL)
* ThreadPoolExecutor
* Python threading
* CSV
* JSON
* SHA256 hashing

The engine also optionally uses `psutil` when available.

---

## 📁 Project Structure

```text
alijinnah_software1/
│
├── engine.py
├── ui.py
└── README.md
```

### `engine.py`

Contains the core processing engine, including:

* Gemini API integration
* Metadata generation
* CSV management
* QA scoring
* API management
* Rate-limit handling
* Retry handling
* Image validation
* State persistence
* Progress tracking
* Failed queue management

### `ui.py`

Contains the Tkinter graphical interface, including:

* Dashboard
* Processing controls
* Failed files page
* Settings
* CSV header customization
* Image preview
* API monitoring
* About section
* Dynamic processing workflow

---

## 📋 Requirements

Python 3.x is required.

The project imports:

```text
google-genai
Pillow
psutil
```

`psutil` and the Google GenAI/Pillow modules are handled by the code with import checks in the engine, although the GUI directly imports Pillow.

---

## 🚀 Installation

### 1. Clone the repository

```bash
git clone https://github.com/alijinnah/alijinnah_software1.git
```

### 2. Enter the project folder

```bash
cd alijinnah_software1
```

### 3. Install required packages

```bash
pip install google-genai pillow psutil
```

### 4. Run the application

```bash
python ui.py
```

---

## 🔑 Gemini API Configuration

The application uses Google Gemini for metadata generation.

API keys can be configured through the application's settings/configuration system.

For better security, API keys can also be provided through environment variables.

Example:

```text
GEMINI_API_KEY_API_1
```

Do **not** publish real API keys inside a public GitHub repository.

---

## 📂 Generated Files

During processing, the application can create files such as:

```text
config.json
config.json.bak
engine.log
engine_state.json
failed_queue.csv
qa_score.csv
progress_status.json
fallback_id_map.json
```

These files contain configuration, processing state, logs, queue information, progress information, and QA results.

---

## 🧠 Processing Workflow

The general workflow is:

```text
Select Design Folder
        ↓
Scan Images
        ↓
Check Already Processed Files
        ↓
Validate Images
        ↓
Check Matching EPS
        ↓
Assign Design Number
        ↓
Select Available Gemini API
        ↓
Generate Metadata
        ↓
Clean & Validate Metadata
        ↓
Calculate QA Score
        ↓
Write CSV Data
        ↓
Verify Written Data
        ↓
Mark Design as Completed
```

The GUI's workflow performs folder scanning, skips files already recorded in CSVs, validates images, checks EPS files, and dispatches the remaining files for processing.

---

## 📌 Current Supported Metadata Outputs

### Adobe Stock

```text
File name
Category
Title
Keywords
People/Property
```

### Shutterstock

```text
Filename
Description
Keywords
Category 1
Category 2
Illustration
Mature Content
Editorial
```

### Vecteezy

```text
Filename
Title
Keywords
License
```

These headers can be customized from the GUI while the three supported platforms remain fixed.

---

## 👨‍💻 Developer

**MD ALI JINNAH**

GitHub: [@alijinnah](https://github.com/alijinnah)

Repository: [alijinnah_software1](https://github.com/alijinnah/alijinnah_software1)

---

## 📌 Project Status

This project is under active development.

New fixes, improvements, and features may be added over time.

---

## 📄 License

No license file is currently included in this repository.
