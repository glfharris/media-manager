addon_dir := `echo ~/.local/share/Anki2/addons21/media-manager-test`
package := "media-manager-anki_v21"

build:
    rm -rf media_manager/__pycache__
    mkdir -p ./build
    cd media_manager && zip -r ../build/{{ package }}.ankiaddon . -x "*__pycache__*"

clean:
    rm -rf ./build

test: testclean
    cp -r ./media_manager {{ addon_dir }}
    anki

testclean:
    rm -rf {{ addon_dir }}
