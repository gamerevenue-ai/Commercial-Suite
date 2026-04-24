import streamlit as st

st.set_page_config(
    page_title="Steam Commercial Suite",
    page_icon="🎮",
    layout="wide",
    initial_sidebar_state="expanded",
)

pages = [
    st.Page("pages/3_Revenue_Optimizer.py", title="Revenue Optimizer", icon="💰"),
]

pg = st.navigation(pages)
pg.run()
