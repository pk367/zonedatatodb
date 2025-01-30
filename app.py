import streamlit as st
import requests
import base64
import os
import importlib
import sys

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
        st.error(f"Failed to fetch the private code. Status code: {response.status_code}, Message: {response.json().get('message')}")
        return None

def main():
    st.title("In 1 Minute Execution")

    # Fetch and import private code
    module_name = fetch_private_code()
    if module_name:
        # Ensure current directory is in the Python path
        sys.path.append(os.getcwd())
        
        # Dynamically import the private code module
        try:
            private_module = importlib.import_module(module_name)
            importlib.reload(private_module)
        except ModuleNotFoundError as e:
            st.error(f"Error importing module: {str(e)}")
            return

        # Get timeframe from Streamlit secrets
        timeframe = st.secrets["INTERVAL"]
        
        # Add button to execute the private module function
        if st.button("Execute"):
            if hasattr(private_module, "fetch_data_endpoint"):
                result = private_module.fetch_data_endpoint(timeframe)
                st.write(result)
            else:
                st.error("Function 'fetch_data_endpoint' not found in the private code.")

if __name__ == "__main__":
    main()
