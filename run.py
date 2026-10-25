from tracker import create_app

app = create_app()

if __name__ == "__main__":
    # host 0.0.0.0 lets phones on the shop wifi reach the app
    app.run(host="0.0.0.0", port=5000, debug=False)
