import streamlit as st
import requests
import base64
import os
import importlib

def fetch_private_code():
    # Fetch the latest code from a private GitHub repo
    GITHUB_TOKEN = st.secrets["GITHUB_TOKEN"]
    REPO_OWNER = "pk367"
    REPO_NAME = "zoneScannerPrivateCode.py"
    FILE_PATH = "privateCode.py"

    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{FILE_PATH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        file_content = base64.b64decode(response.json()["content"]).decode("utf-8")
        with open("privateCode.py", "w") as file:
            file.write(file_content)
        return "privateCode"
    else:
        st.error("Failed to fetch the private code.")
        return None

def main():
    st.title("in 1 minute")

    # Fetch and import private code
    module_name = fetch_private_code()
    if module_name:
        # Dynamically import the private code module
        private_module = importlib.import_module(module_name)

        # Get timeframe from Streamlit secrets
        timeframe = st.secrets["INTERVAL"]
        
        # Add button to execute the private module function
        if st.button("Execute"):
            result = private_module.execute_for_timeframe(timeframe)
            st.write(result)

if __name__ == "__main__":
    main()
