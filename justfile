addon_dir := `echo ~/.local/share/Anki2/addons21/media-manager-test`

build:
    mkdir -p ./build
    zip -r -j ./build/media-manager-anki_v21.zip media_manager

clean:
    rm -rf ./build

test: testclean
    cp -r ./media_manager {{ addon_dir }}
    anki

testclean:
    rm -rf {{ addon_dir }}
