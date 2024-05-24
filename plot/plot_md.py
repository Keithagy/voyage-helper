import os
import re
import plotly.graph_objects as go
import chart_studio

chart_studio.tools.set_credentials_file(username='tbsfchnr', api_key='YGDrRHkapBeeGqY3nZcp')

# Get a list of all Markdown files in the current directory
md_files = [file for file in os.listdir() if file.endswith('.md')]

# Loop through each Markdown file
for md_file in md_files:
    with open(md_file, 'r') as f:
        md_content = f.read()

    # Extract data from Markdown content using regular expressions
    matches = re.findall(r'\|\s*([\w\s]+?)\s*\|\s*(\d+)\s*\|', md_content)

    # Extracting day and hours data from matches
    days = [match[0] for match in matches]
    hours = [int(match[1]) for match in matches]

    print(md_file, days, hours)

    # Create polar chart
    fig = go.Figure(data=[go.Scatterpolar(
        r=hours,
        theta=days,
        fill='toself'
    )])

    fig.update_layout(
        polar=dict(
            radialaxis=dict(
                visible=True,
            ),
        ),
        showlegend=False,
    )

    fig.update_polars(angularaxis_direction="clockwise")

    fig.update_traces(hovertemplate="Hours: %{r} <extra>1: connections to task cards\n2: mentions of co-workers</extra>", line_shape="spline")

    # Write HTML export to Plotly Chart Studio
    url = chart_studio.plotly.plot(fig, filename=md_file.split('.')[0], auto_open=False)
    
    # Embed URL in an iframe and replace it in the Markdown content
    iframe_tag = f'<iframe src="{url}" width="100%" height="400px"></iframe>'
    
    # Read existing content from the Markdown file
    with open(md_file, 'r') as f:
        md_content = f.readlines()

    # Check if the last line is an iframe tag
    if md_content and md_content[-1].strip().startswith('<iframe'):
        # Replace the last line with the new iframe tag
        md_content[-1] = iframe_tag + '\n'
    else:
        # Append the new iframe tag
        md_content.append(iframe_tag + '\n')

    # Write the modified Markdown content back to the file
    with open(md_file, 'w') as f:
        f.writelines(md_content)
